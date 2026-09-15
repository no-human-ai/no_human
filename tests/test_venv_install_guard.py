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

import contextlib
import logging
import os
import shutil
import stat
import tempfile

import pytest

from no_human.agent import exec_names, guard, venv_install_guard

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


@pytest.fixture(autouse=True)
def _clear_fold_cache():
    """`host_folds_case` is `lru_cache`d per-process; without this a test that
    pins the probe (via monkeypatch) or measures a real `tmp_path` volume can
    read a stale answer left behind by a previous test in this file.
    """
    clear = getattr(exec_names.host_folds_case, "cache_clear", None)
    if clear is not None:
        clear()
    yield
    clear = getattr(exec_names.host_folds_case, "cache_clear", None)
    if clear is not None:
        clear()


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


def _mkvenv(root, extra_names=()):
    """A real, executable-bit venv layout: <root>/pyproject.toml +
    <root>/.venv/{pyvenv.cfg,bin/{python,pip,uv,uvx,...}}.

    `extra_names`: additional literal file spellings to also create in
    `bin/` (e.g. `("PIP", "Pip")`) — for a test that mocks `host_folds_case`
    to simulate a folding host, the mock only changes the CLASSIFICATION
    decision; it cannot make a genuinely case-sensitive test-runner
    filesystem (Linux/ext4 CI) resolve a literal `"PIP"` PATH lookup against
    a file that is really named `pip`. On a REAL folding host the OS itself
    resolves that lookup for free; simulating "what a real folding host
    would see" on a case-sensitive runner requires the literal spelling to
    actually exist on disk."""
    venv = os.path.join(root, ".venv")
    bindir = os.path.join(venv, "bin")
    os.makedirs(bindir, exist_ok=True)
    with open(os.path.join(venv, "pyvenv.cfg"), "w") as f:
        f.write("home = /usr/bin\n")
    for name in ("python", "python3", "pip", "pip3", "uv", "uvx", *extra_names):
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


def _session(tmp_path, primary_extra_names=()):
    """Real two-tree layout: `primary/` (the shared dev checkout a coder
    session must never write into) and `wt/` (the session's own worktree).
    `primary_extra_names` forwards to `_mkvenv` for `primary/` only — the
    tree resolution actually walks via `PATH` in the production shape below
    (see `prod_env`)."""
    primary, primary_venv = _mkvenv(tmp_path / "primary", extra_names=primary_extra_names)
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


def test_a_capitalised_uv_commands_are_not_denied_like_pip(tmp_path, monkeypatch):
    """Regression (case-fold review, BLOCKER 2): the exclusion that keeps
    `uv`/`uvx`'s own resolved binary from being mistaken for an install
    TARGET (they resolve targets via cwd/`pyproject.toml`, not their own
    location) used to compare `_basename(exe) in ("uv", "uvx")` verbatim
    against the SAME `installers` list that `_is_installer_name` populates
    by folding case wherever the host folds it. That disagreed with its own
    upstream classifier: on a folding host, `UV sync` was recognised as an
    installer invocation but not matched by this bare membership test, so it
    fell through to the pip/python "owning venv" path and was DENIED even
    though `uv sync` (lowercase) — doing the identical thing — was ALLOWED.
    Pins the ablation-confirmed one-line fix (`.lower()` on both sides of
    the membership test) by exercising every case variant, for both `sync`
    and `add` (`add` is in `_MUTATING_SUBCOMMANDS` too and hits the exact
    same exclusion), on a host pinned to fold, where the bug was actually
    observable.

    Measured directly against this repo's history: `uv add somepkg`
    (lowercase) is allowed at every commit checked, including the pre-task
    baseline (680d6889) and the pre-BLOCKER-2-fix commit (0762ac0e) —
    `uv`/`uvx` never resolve an install target via their OWN binary
    location (see the long comment above the exclusion this test pins), so
    a foreign shared `VIRTUAL_ENV` never makes `uv add`/`uv sync` resolve
    outside a worktree that owns its own `pyproject.toml`, by design and
    regardless of case. `UV add somepkg` diverging from that (denied, where
    lowercase was not) at 0762ac0e was this same BLOCKER-2 shape, just
    surfaced through a second `_MUTATING_SUBCOMMANDS` entry.
    """
    monkeypatch.setattr(exec_names, "host_folds_case", lambda *a, **k: True)
    # `primary_extra_names` -- without it, on a case-sensitive test-runner
    # filesystem (Linux/ext4 CI) no file literally named `UV`/`Uv`/`uV`
    # exists in `primary_venv/bin/`, so `_resolve_installer`'s PATH walk
    # fails to resolve those spellings at all and falls open (allows with
    # a "could not be resolved" log) regardless of whether the fix under
    # test does anything -- the assertion below would pass for the wrong
    # reason and the fold logic would go unexercised on that host.
    _primary, _primary_venv, wt, _wt_venv, prod_env, _wt_env = _session(
        tmp_path, primary_extra_names=("UV", "Uv", "uV"))
    cmds = [
        "uv sync", "UV sync", "Uv sync", "uV sync",
        "uv add somepkg", "UV add somepkg", "Uv add somepkg",
    ]
    for cmd in cmds:
        r = venv_install_guard.denial_reason(cmd, cwd=wt, env=prod_env)
        assert r is None, (
            f"a capitalised `uv` command must be allowed exactly like the "
            f"lowercase spelling on a folding host: {cmd!r} — {r}")
        d = _ev("Bash", {"command": cmd}, cwd=wt, env=prod_env)
        assert d.allow, f"must stay allowed via evaluate(): {cmd!r} — {d.reason}"


def test_a_capitalised_uvx_program_flag_is_not_denied_like_pip(tmp_path, monkeypatch):
    """Regression (case-fold review, BLOCKER A): `_uses_active_env`'s
    `expects_program` test — whether a trailing flag belongs to the invoked
    PROGRAM rather than to `uv`/`uvx` itself — compared
    `_basename(tokens[start]).startswith("uvx")` verbatim, un-folded, even
    though the classifier that puts this command on the installer path at
    all (`_is_installer_name`) already folds case wherever the host folds
    it. That disagreed with its own upstream classifier exactly like
    BLOCKER 2's `uv`/`uvx` exclusion did: on a folding host, `UVX ruff check
    --active` was recognised as an installer invocation but `expects_program`
    stayed `False` (the bare `.startswith("uvx")` does not match `"UVX"`),
    so `--active` was read as uv's OWN flag and the command was DENIED —
    while the identical `uvx ruff check --active` (lowercase) stayed
    ALLOWED, because for it `expects_program` correctly saw `ruff` as the
    invoked program and treated the trailing `--active` as ruff's, not
    uv's. Pins the `.lower()` fix mirroring the already-correct sibling
    exclusion above (`_basename(exe).lower() in ("uv", "uvx")`)."""
    monkeypatch.setattr(exec_names, "host_folds_case", lambda *a, **k: True)
    # `primary_extra_names` creates the literal `UVX`/`Uvx` files in the
    # shared venv's `bin/` -- mocking `host_folds_case` only changes the
    # CLASSIFICATION decision; on a genuinely case-sensitive test-runner
    # filesystem (Linux/ext4 CI) `_resolve_installer`'s real PATH walk still
    # needs the literal spelling to exist on disk to resolve it the way a
    # real folding host's shell would for free (same pattern as
    # `test_a_capitalised_installer_is_refused_on_a_folding_cwd` above).
    _primary, _primary_venv, wt, _wt_venv, prod_env, _wt_env = _session(
        tmp_path, primary_extra_names=("UVX", "Uvx"))
    cmds = ["uvx ruff check --active", "UVX ruff check --active",
            "Uvx ruff check --active"]
    for cmd in cmds:
        r = venv_install_guard.denial_reason(cmd, cwd=wt, env=prod_env)
        assert r is None, (
            f"a trailing --active after the invoked program belongs to that "
            f"program, not to uvx, regardless of uvx's own case: {cmd!r} — {r}")
        d = _ev("Bash", {"command": cmd}, cwd=wt, env=prod_env)
        assert d.allow, f"must stay allowed via evaluate(): {cmd!r} — {d.reason}"
    # And the sibling shape (--active BEFORE the program) stays denied
    # regardless of case, exactly like the all-lowercase spelling already
    # pinned in test_the_full_bypass_set_stays_denied.
    for cmd in ["uvx --active ruff", "UVX --active ruff", "Uvx --active ruff"]:
        r = venv_install_guard.denial_reason(cmd, cwd=wt, env=prod_env)
        assert r is not None, (
            f"uvx has no --active flag at all -- `uvx --active ruff "
            f"--version` errors with \"unexpected argument '--active' "
            f"found\" before running anything (measured, uv 0.12.5) -- so "
            f"denying refuses nothing anyone is entitled to run: {cmd!r}")


def test_the_cwd_argument_is_actually_threaded_to_the_probe(tmp_path, monkeypatch):
    """Mutation-pinning (case-fold review, M3), entry-point level.

    `_is_installer_name` takes an explicit `cwd=` at every one of its 8
    call sites in this module so the fold probe answers from the SESSION's
    own working directory, not from wherever the orchestrator process
    itself happens to be sitting. This test pins ONE of those sites — the
    literal-token gate in `_resolve_installer` (`venv_install_guard.py`,
    reached here because `"PIP"` has no `/`) — via the real, public
    `denial_reason` entry point. Narrowed from an earlier claim that this
    single test pinned "each" call site: it does not, and cannot, because
    several other call sites are only reached *after* this same gate
    already returned a verdict — e.g. the PATH-walk's own re-checks at
    lines 832/857 sit inside the same `_resolve_installer` call and this
    test's own control flow never reaches them (a correctly-threaded
    `cwd=wt` returns at line 683, before the PATH walk even starts); a
    dropped `cwd` at 832/857 would therefore be masked by 683 answering
    correctly, exactly the redundant-enumeration trap
    `test_mutating_subcommand_threads_cwd_to_the_inner_installer_skip`
    calls out for `denial_reason`'s own outer loop. That test independently
    pins a second, walker-level site (`_mutating_subcommand`'s inner skip
    at line 943) by calling the private helper directly, bypassing this
    gate entirely so it cannot be masked the same way; the test below this
    one pins a third, similarly isolated site
    (`_spaced_path_candidates`, line 435). The remaining sites (652, 670,
    832, 857) share `_is_installer_name`'s single implementation and the
    same `(name, cwd)` call shape as the three sites pinned here — reviewed
    by inspection to thread `cwd` correctly — but are not each
    independently mutation-tested by this round: 670 gates only a WARNING
    log, not the allow/deny verdict, and 1042 (`_uses_active_env`'s own
    installer skip) is unreachable in a way a test could observe at all —
    its condition is `_is_installer_name(tok, cwd) and not expects_program`,
    and every branch of that `and` converges on the identical `i += 1` the
    surrounding walk already does on the other side of the check, so no
    input can make `_is_installer_name`'s answer at that specific site
    change the function's return value, dropped `cwd` or not.

    Isolates `cwd` as the only anchor whose fold answer is real: every
    directory except the session's own worktree measures `None`
    (unmeasurable, including every real `PATH` entry and the test process's
    own real `os.getcwd()`), only `wt` is pinned to a determinate `False`
    (non-folding). `env["PATH"]` is left as `prod_env`'s real value
    (pointing at `primary_venv`) so command *resolution* still works
    end-to-end -- only the fold *measurement* is faked. Because the
    union-of-anchors probe returns `False` (rather than falling through to
    the fail-closed default) the instant ANY anchor determinately answers
    `False`, a correctly cwd-threaded call sees the non-folding answer and
    treats `PIP` as a distinct program from `pip` (allowed, `pip` is not
    even considered an installer name so `PIP install evilpkg` is never
    compared against `primary_venv` at all); a call that dropped `cwd`
    never reaches that anchor, sees only `None` answers from the real
    anchors it falls back to, lands on the fail-closed default `True`
    (fold), and then denies because `PIP` resolves as `pip` pointing
    outside the worktree.
    """
    # `primary_extra_names=("PIP",)` -- without a literal `PIP` file in
    # `primary_venv/bin/` on a case-sensitive test-runner filesystem, the
    # PATH hand-walk (`os.path.join(directory, token)`, no fold-aware
    # scan) can never resolve `PIP` to `primary_venv`'s real `pip` even in
    # the BUGGY (cwd-not-threaded) branch, so both branches fall through to
    # the same allow-and-log outcome and the assertion below would pass
    # for the wrong reason (same class as the `UV`/`Uv` omission this
    # review flagged in `test_a_capitalised_uv_commands_are_not_denied_like_pip`).
    _primary, _primary_venv, wt, _wt_venv, prod_env, _wt_env = _session(
        tmp_path, primary_extra_names=("PIP",))

    def _pinned(directory):
        return False if os.path.realpath(directory) == wt else None

    monkeypatch.setattr(exec_names, "_folds_case_at", _pinned)
    exec_names.host_folds_case.cache_clear()

    denied = venv_install_guard.denial_reason(
        "PIP install evilpkg", cwd=wt, env=prod_env
    )
    assert denied is None, (
        "cwd=wt must be threaded into the probe and measure False "
        "(non-folding), so `PIP` is a distinct program from `pip` and stays "
        f"allowed; got denial: {denied!r}"
    )


def test_mutating_subcommand_threads_cwd_to_the_inner_installer_skip(
    tmp_path, monkeypatch
):
    """Mutation-pinning (case-fold review, M3), the token-walker level.

    `denial_reason`'s own `resolved_positions` enumeration independently
    re-checks every token against `_resolve_installer(tok, cwd, env)` with
    the correct `cwd` already threaded (that call site was never the
    problem), so a command whose inner installer-name token is ALSO
    followed by a literal mutating word (`uv PIP install foo`) is denied
    either way and cannot tell the two code paths apart — the redundant,
    already-correct enumeration masks a broken inner-skip. This test goes
    straight at the walker instead of through `denial_reason`, so nothing
    can mask it.

    `_mutating_subcommand` skips right past a token it recognises as an
    installer name — that's how `uv pip install foo` finds `install` and
    not `pip` as uv's own adjacent subcommand (`uv pip install` is a
    sub-invocation prefix, not the subcommand itself). Pinned so `wt`
    folds (`True`) and every other real anchor (this test process's own
    real `os.getcwd()`, every real `PATH` entry) determinately does NOT
    (`False`, never `None` — no fail-closed default available to hide a
    dropped `cwd` behind). With `cwd` correctly threaded into that skip's
    `_is_installer_name` call, `PIP` folds to `pip` on `wt`, is recognised
    as the inner installer name, gets skipped, and the walk lands on
    `install`. A call that silently dropped `cwd` (or accepted it and
    never wired it through) measures the real anchors instead, lands on
    the determinate `False`, never recognises `PIP` as an installer name,
    stops the scan there, and returns the literal token `"PIP"` — not a
    member of `_MUTATING_SUBCOMMANDS`, silently losing intent.
    """
    wt = str(tmp_path / "wt")
    os.makedirs(wt)

    def _pinned(directory):
        return os.path.realpath(directory) == os.path.realpath(wt)

    monkeypatch.setattr(exec_names, "_folds_case_at", _pinned)
    exec_names.host_folds_case.cache_clear()

    tokens = ["uv", "PIP", "install", "foo"]
    subcommand = venv_install_guard._mutating_subcommand(tokens, 0, wt)
    assert subcommand == "install", (
        "cwd=wt must be threaded through the inner-installer-name skip so "
        "`PIP` folds to `pip` (recognised, skipped) and the walk lands on "
        f"`install`; got {subcommand!r}"
    )


def test_spaced_path_candidates_threads_cwd_to_the_installer_check(
    tmp_path, monkeypatch
):
    """Mutation-pinning (case-fold review, M3), a third independently-isolated
    site: `_spaced_path_candidates` (line 435) is a standalone function
    callable directly, so — like
    `test_mutating_subcommand_threads_cwd_to_the_inner_installer_skip` above
    — nothing upstream can mask a dropped `cwd` here the way the
    `_resolve_installer` PATH-walk's own re-checks are masked by its
    earlier literal-token gate (see the docstring of
    `test_the_cwd_argument_is_actually_threaded_to_the_probe`).

    `_spaced_path_candidates` rebuilds the leading prefixes a nested
    `cmd /c "..."` payload's re-lex could have destroyed (issue #105 round
    3) and keeps only the ones that name an installer. The payload here is
    already in the `/`-normalised spelling `win_readings.readings` would
    have produced from a real backslashed Windows path; its installer
    token is spelled `PIP`. Pinned exactly like the sibling test: `wt`
    folds (`True`), every other real anchor determinately does not
    (`False`, never `None`, so a dropped `cwd` cannot hide behind the
    fail-closed default). With `cwd` correctly threaded, `PIP` folds to
    `pip`, is recognised as the installer, and the joined prefix is kept;
    a dropped `cwd` measures the real anchors, `PIP` is never recognised,
    and the payload is dropped, producing an empty candidate list.
    """
    wt = str(tmp_path / "wt")
    os.makedirs(wt)

    def _pinned(directory):
        return os.path.realpath(directory) == os.path.realpath(wt)

    monkeypatch.setattr(exec_names, "_folds_case_at", _pinned)
    exec_names.host_folds_case.cache_clear()

    payload = "C:/Program Files/proj/.venv/Scripts/PIP install requests"
    candidates = venv_install_guard._spaced_path_candidates(payload, wt)
    assert candidates == [
        "C:/Program Files/proj/.venv/Scripts/PIP", "install", "requests",
    ], (
        "cwd=wt must be threaded through so `PIP` folds to `pip` "
        f"(recognised as the installer); got {candidates!r}"
    )


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
def test_an_unreadable_venv_is_not_escaped_by_a_system_installer_later_on_path(tmp_path):
    """The regression that turned `main` red and that
    `test_an_unreadable_venv_pyvenv_cfg_still_denies_the_install` could not
    see: that test inherits `/usr/bin:/bin` from `_session`, so whether a
    SECOND, determinate `pip` exists behind the unreadable venv is a
    property of the HOST. `/usr/bin/pip` exists on the Ubuntu CI runner and
    does not exist on the macOS dev machine, so the bare-token case proved
    the fail-open was closed locally while it was still wide open on CI.

    This test owns both `PATH` entries instead of inheriting one, so the
    condition is the same on every host: an unreadable venv first, and a
    determinate `pip` that belongs to NO venv immediately behind it. Without
    the fix the walk skips the unstat'able venv, returns the system `pip`,
    finds it owns no venv, and allows the install — reporting "no venv is
    involved", a positive claim it never established, about a `PATH` entry it
    could not read. The `chmod` buys the ALLOW, which is exactly what this
    ticket exists to prevent.

    Deliberately NOT justified by `VIRTUAL_ENV`: `pip` does not read it (it
    decides on `sys.prefix != sys.base_prefix`) and `uv sync` ignores it with
    a warning. See the DO-NOT-RESTATE paragraph in `_resolve_installer`."""
    primary, primary_venv, wt, wt_venv, prod_env, wt_env = _session(tmp_path)
    sysbin = tmp_path / "sysbin"
    sysbin.mkdir()
    system_pip = sysbin / "pip"
    system_pip.write_text("#!/bin/sh\nexit 0\n")
    os.chmod(system_pip, 0o755)
    # `sysbin` is a plain directory, NOT a venv: no pyvenv.cfg anywhere above
    # it, so `_venv_root_of` answers None for it. That is the whole point --
    # a winner that owns no venv cannot say where the install lands.
    assert venv_install_guard._venv_root_of(str(system_pip)) is None

    env = {
        "PATH": f"{primary_venv}/bin{os.pathsep}{sysbin}",
        "VIRTUAL_ENV": primary_venv,
    }
    cmd = "pip install evilpkg"

    before = venv_install_guard.denial_reason(cmd, cwd=wt, env=env)
    assert before is not None, "positive control: denied while the venv is readable"
    assert primary_venv in before

    with _unreadable(primary_venv):
        resolved = venv_install_guard._resolve_installer("pip", wt, env)
        assert resolved == os.path.join(primary_venv, "bin", "pip"), (
            "an undetermined venv entry must not be displaced by a LATER "
            f"match that belongs to no venv; got {resolved!r}"
        )
        inside = venv_install_guard.denial_reason(cmd, cwd=wt, env=env)
        assert inside is not None, (
            "REGRESSION: a chmod'd venv plus a system pip behind it on PATH "
            "must still deny the install"
        )
        assert primary_venv in inside
        d = _ev("Bash", {"command": cmd}, cwd=wt, env=env)
        assert not d.allow, f"must still be blocked via evaluate(): {d.reason}"

    after = venv_install_guard.denial_reason(cmd, cwd=wt, env=env)
    assert after is not None, "must stay denied once permissions are restored"


@requires_chmod
def test_an_unreadable_non_venv_path_entry_does_not_displace_a_system_installer(tmp_path):
    """The other half of the condition, and the reason it is not simply
    "prefer the undetermined entry". An unreadable `PATH` entry that is
    DETERMINATELY not inside a venv carries no information about where an
    install would land, so preferring it over a determinate match would only
    discard a real answer.

    The word DETERMINATELY is load-bearing and this test only covers the
    shallow shape. `_venv_root_of` treats an UNDETERMINED `pyvenv.cfg` probe
    as a venv root, so a non-venv directory whose own PARENT is unreadable
    reads as a venv and does displace the system match. That is fail-closed
    and accepted; it is recorded in the module comment rather than claimed
    away. Here the chmod is on the pip's immediate parent, whose own parent
    (`tmp_path`) stays readable, so the probe two levels up is a determinate
    False."""
    primary, primary_venv, wt, wt_venv, prod_env, wt_env = _session(tmp_path)
    opaque = tmp_path / "opaque"
    opaque.mkdir()
    decoy = opaque / "pip"
    decoy.write_text("#!/bin/sh\nexit 0\n")
    os.chmod(decoy, 0o755)
    assert venv_install_guard._venv_root_of(str(decoy)) is None

    sysbin = tmp_path / "sysbin"
    sysbin.mkdir()
    system_pip = sysbin / "pip"
    system_pip.write_text("#!/bin/sh\nexit 0\n")
    os.chmod(system_pip, 0o755)

    env = {"PATH": f"{opaque}{os.pathsep}{sysbin}"}
    with _unreadable(opaque):
        resolved = venv_install_guard._resolve_installer("pip", wt, env)
        assert resolved == os.path.realpath(str(system_pip)), (
            "an unreadable NON-venv entry must not displace a determinate "
            f"match; got {resolved!r}"
        )


@requires_chmod
def test_a_non_venv_undetermined_entry_does_not_shadow_a_later_undetermined_venv(tmp_path):
    """The bypass an independent review found in the first version of this
    fix. The walk remembered ONE undetermined candidate — the first one with
    an installer NAME — and then asked whether it owned a venv. Those are not
    the same question. A single unreadable non-venv directory placed ahead of
    the unreadable venv claims that slot, answers "no venv", and the venv's
    evidence is dropped: the guard resolves to the system `pip` and ALLOWS,
    which is the exact DENY->ALLOW the ticket exists to close, reachable by
    adding one PATH entry.

    Both undetermined entries are built here, non-venv FIRST, so the test
    fails if the code ever goes back to tracking a single fallback."""
    primary, primary_venv, wt, wt_venv, prod_env, wt_env = _session(tmp_path)

    opaque = tmp_path / "opaque"
    opaque.mkdir()
    (opaque / "pip").write_text("#!/bin/sh\nexit 0\n")
    os.chmod(opaque / "pip", 0o755)
    # Determinately NOT a venv: its parent is readable and holds no
    # pyvenv.cfg, so the probe two levels up is a real False, not undetermined.
    assert venv_install_guard._venv_root_of(str(opaque / "pip")) is None

    sysbin = tmp_path / "sysbin"
    sysbin.mkdir()
    (sysbin / "pip").write_text("#!/bin/sh\nexit 0\n")
    os.chmod(sysbin / "pip", 0o755)

    env = {
        "PATH": os.pathsep.join(
            [str(opaque), f"{primary_venv}/bin", str(sysbin)]),
        "VIRTUAL_ENV": primary_venv,
    }
    cmd = "pip install evilpkg"

    with _unreadable(opaque), _unreadable(primary_venv):
        resolved = venv_install_guard._resolve_installer("pip", wt, env)
        assert resolved == os.path.join(primary_venv, "bin", "pip"), (
            "an unreadable NON-venv entry must not consume the slot that the "
            "unreadable VENV entry needs; the venv is the only entry carrying "
            f"evidence of a target. got {resolved!r}"
        )
        inside = venv_install_guard.denial_reason(cmd, cwd=wt, env=env)
        assert inside is not None, (
            "REGRESSION: one extra unreadable non-venv PATH entry re-opened "
            "the chmod bypass"
        )
        assert primary_venv in inside
        d = _ev("Bash", {"command": cmd}, cwd=wt, env=env)
        assert not d.allow, f"must still be blocked via evaluate(): {d.reason}"


@requires_chmod
def test_the_sessions_own_unreadable_venv_does_not_shadow_a_foreign_one(tmp_path):
    """The SECOND instance of the first-one-wins bug, one level in from the
    first, found by an independent review after the first was fixed.

    Remembering "the first undetermined entry that owns a venv" repeats the
    same mistake it was introduced to fix: when TWO entries are equally
    indeterminate, PATH ORDER ALONE decides ALLOW vs DENY between candidates
    the guard has admitted it cannot read. Measured on the same two
    directories: own venv first gave ALLOW, foreign venv first gave DENY.

    The bad ordering is the ORDINARY production one, not a contrived PATH.
    `uv run` prepends the project's own venv ahead of the inherited shared
    venv, so a coder session running under it produces own-venv-first for
    free, and chmod'ing both venvs — one of which it owns outright — buys
    back the bypass this whole ticket exists to close.

    The rule that resolves it is not positional: among candidates that cannot
    be read, prefer the one that would be DENIED. A venv inside `cwd` would
    be waved through, so keeping it discards the only candidate whose verdict
    differs from the default."""
    primary, primary_venv, wt, wt_venv, prod_env, wt_env = _session(tmp_path)
    # BOTH shapes, because they leave `_resolve_installer` by different
    # doors and each door had to be fixed separately. With a determinate
    # system `pip` behind them the displacement branch decides; with nothing
    # determinate anywhere the end-of-scan remembered-candidate path decides.
    # The first version of this fix closed only the former, and the latter
    # still returned the session's own venv and ALLOWED.
    sysbin = tmp_path / "sysbin"
    sysbin.mkdir()
    (sysbin / "pip").write_text("#!/bin/sh\nexit 0\n")
    os.chmod(sysbin / "pip", 0o755)

    cmd = "pip install evilpkg"
    shapes = {
        "a determinate system pip behind both venvs": [
            f"{wt_venv}/bin", f"{primary_venv}/bin", str(sysbin)],
        "nothing determinate anywhere on PATH": [
            f"{wt_venv}/bin", f"{primary_venv}/bin"],
    }
    for label, parts in shapes.items():
        env = {"PATH": os.pathsep.join(parts), "VIRTUAL_ENV": wt_venv}
        with _unreadable(wt_venv), _unreadable(primary_venv):
            resolved = venv_install_guard._resolve_installer("pip", wt, env)
            assert resolved == os.path.join(primary_venv, "bin", "pip"), (
                f"with {label}: among two unreadable venvs the guard must "
                "keep the one it would DENY, not whichever PATH happened to "
                f"list first; got {resolved!r}"
            )
            inside = venv_install_guard.denial_reason(cmd, cwd=wt, env=env)
            assert inside is not None, (
                f"REGRESSION with {label}: the session's own unreadable venv "
                "shadowed the foreign one and bought back the chmod bypass"
            )
            assert primary_venv in inside
            d = _ev("Bash", {"command": cmd}, cwd=wt, env=env)
            assert not d.allow, f"{label}: blocked via evaluate(): {d.reason}"


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


# ---------------------------------------------------------------------------
# Item 4 (mutation-testing gaps, round-2 review): `_effective_prefixes` has
# two more sites that changed from a bare stdlib stat call to the tri-state
# `_probe_is_file`/`_probe_is_dir` contract, and neither had a test that
# would fail if `is not False` were mutated to plain truthiness (which
# folds `None` back into "falsy" and reintroduces the exact fail class this
# ticket exists to close).
# ---------------------------------------------------------------------------

def test_python_flag_owning_venv_is_still_found_when_its_probe_is_undetermined(tmp_path, monkeypatch):
    """Mutation-kill for `_effective_prefixes`'s `--python`/`-p` handling
    (`owning = _venv_root_of(joined) if _probe_is_file(real) is not False
    else None`). `real` is the FULLY symlink-followed path (potentially deep
    in an out-of-tree uv cache); `joined` is the value AS NAMED (still
    in-tree, e.g. `<wt>/.venv/bin/python3`). When `real` cannot be stat'd,
    the probe returns `None`, and the fix's `is not False` still takes the
    `_venv_root_of` branch — finding the worktree's OWN venv via `joined`
    and adding that in-tree root as the candidate. A mutant that used bare
    truthiness (or `is True`) would treat `None` as "not a file", skip
    `_venv_root_of` entirely, and fall back to adding `real` itself — the
    out-of-tree cache path — turning a legitimate in-tree install into a
    false DENY.

    A real `chmod` cannot isolate this line by itself: the generic
    path-like-token loop a few lines down (`_probe_is_dir(real) is not
    False`, the OTHER tri-state site in this function) stats the exact same
    `real` path, so a permission failure that makes `_probe_is_file`
    undetermined makes `_probe_is_dir` undetermined too — and that other
    loop then independently (and correctly, per ITS OWN fail-closed
    contract) adds the unstat'able path as a candidate, denying the install
    regardless of what this line decides. That masks this line's own
    contribution rather than testing it — confirmed empirically: chmod-ing
    the cache directory denies the install both before and after reverting
    just this line, so a chmod-based version of this test cannot
    distinguish fixed from mutant.

    `_probe_is_file` is instead monkeypatched to return `None` for exactly
    the resolved cache path, leaving `_probe_is_dir` genuinely untouched —
    it stats the (fully readable) file for real and correctly reports
    `False` ("not a directory"), so the generic loop does not also add it,
    and only this line's own branch selection is under test."""
    primary, primary_venv, wt, wt_venv, prod_env, wt_env = _session(tmp_path)
    cache_dir = tmp_path / "cache-uv" / "python3.12"
    cache_dir.mkdir(parents=True)
    interpreter = cache_dir / "python3"
    interpreter.write_text("#!/bin/sh\nexit 0\n")
    st = os.stat(interpreter)
    os.chmod(interpreter, st.st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    linked = os.path.join(wt_venv, "bin", "python3")
    os.remove(linked)
    os.symlink(str(interpreter), linked)
    real_target = os.path.realpath(linked)
    cmd = f"uv pip install --python {linked} foo"

    r = venv_install_guard.denial_reason(cmd, cwd=wt, env=wt_env)
    assert r is None, f"positive control: unobstructed cache symlink stays allowed: {r}"

    real_probe_is_file = venv_install_guard._probe_is_file

    def stub(path):
        if path == real_target:
            return None
        return real_probe_is_file(path)

    monkeypatch.setattr(venv_install_guard, "_probe_is_file", stub)

    undetermined = venv_install_guard.denial_reason(cmd, cwd=wt, env=wt_env)
    assert undetermined is None, (
        "REGRESSION: an undetermined probe on the symlink-followed cache "
        f"path must not defeat resolution of the in-tree venv named by "
        f"the value as given: {undetermined}"
    )
    d = _ev("Bash", {"command": cmd}, cwd=wt, env=wt_env)
    assert d.allow, f"must be allowed via evaluate(): {d.reason}"


@requires_chmod
def test_an_unstattable_cd_target_outside_the_worktree_still_denies(tmp_path):
    """Mutation-kill for `_effective_prefixes`'s path-like-token handling
    (`if real and _probe_is_dir(real) is not False:`), the structural
    stand-in for `cd`/`pushd`/subshell-group operands. An out-of-worktree
    directory named by a `cd` token must still count as a write candidate
    even when it cannot be stat'd (its parent made unreadable) — dropping
    an undetermined directory candidate is exactly the fail-open this
    ticket exists to close, just reached through the OTHER tri-state site
    in this function rather than through a venv probe. A mutant that used
    bare truthiness here (`None` is falsy) would silently exclude the
    unstat'able target and let the install through."""
    primary, primary_venv, wt, wt_venv, prod_env, wt_env = _session(tmp_path)
    outer = tmp_path / "outer"
    target = outer / "somewhere"
    target.mkdir(parents=True)
    cmd = f"cd {target} && uv sync"

    r = venv_install_guard.denial_reason(cmd, cwd=wt, env=wt_env)
    assert r is not None, f"positive control: an out-of-worktree cd target is denied: {cmd}"

    with _unreadable(outer):
        inside = venv_install_guard.denial_reason(cmd, cwd=wt, env=wt_env)
        assert inside is not None, (
            "REGRESSION: an unstat'able out-of-worktree cd target must still "
            f"be denied, not silently dropped from the candidate set: {cmd}"
        )
        d = _ev("Bash", {"command": cmd}, cwd=wt, env=wt_env)
        assert not d.allow, f"must still be blocked via evaluate(): {cmd}"

    after = venv_install_guard.denial_reason(cmd, cwd=wt, env=wt_env)
    assert after is not None, f"must stay denied once permissions are restored: {cmd}"


def test_an_unreadable_path_entry_ahead_of_the_sessions_own_venv_is_skipped_not_denied(tmp_path):
    """Blocker 1: `_resolve_installer`'s bare-token `PATH` walk used to
    RETURN on the very first candidate whose file type could not be
    determined, instead of continuing to look for a later, determinate
    match — a POSIX shell's own `command -v`/`type -p` SKIP an EACCES `PATH`
    entry and keep walking, so a shell would resolve `pip` straight past an
    unreadable decoy to the session's own venv, and this guard must agree
    with what would actually execute.

    Measured before the fix: a decoy venv placed AHEAD of the session's own
    venv on `PATH`, with its `bin/` made unreadable, made `_resolve_installer`
    return the decoy path (undetermined, but returned immediately) instead
    of the determinate match one entry later — resolving to a path that
    could never execute, and DENYING an install that was actually headed
    into the coder's own worktree venv. That is a false DENY, the same
    fail-open class this ticket exists to close, just pointed the other way:
    "could not determine" must not silently win over a real, later match
    either."""
    primary, primary_venv, wt, wt_venv, prod_env, wt_env = _session(tmp_path)
    decoy, decoy_venv = _mkvenv(tmp_path / "decoy")
    env = {
        "PATH": f"{decoy_venv}/bin{os.pathsep}{wt_venv}/bin{os.pathsep}/usr/bin{os.pathsep}/bin",
        "VIRTUAL_ENV": wt_venv,
    }
    cmd = "pip install foo"

    # Positive control: with the decoy fully readable, direct resolution to
    # it — not the session's own venv — proves PATH order is what is being
    # exercised (the decoy really is found first when nothing hides it).
    resolved = venv_install_guard._resolve_installer("pip", wt, env)
    assert resolved == os.path.join(decoy_venv, "bin", "pip"), (
        "positive control: an untouched decoy earlier on PATH must resolve first"
    )

    with _unreadable(decoy_venv):
        resolved = venv_install_guard._resolve_installer("pip", wt, env)
        assert resolved == os.path.join(wt_venv, "bin", "pip"), (
            "REGRESSION: an undetermined earlier PATH entry must not pre-empt "
            f"a later determinate match; got {resolved!r}"
        )
        r = venv_install_guard.denial_reason(cmd, cwd=wt, env=env)
        assert r is None, f"must resolve past the unreadable decoy and stay allowed: {r}"
        d = _ev("Bash", {"command": cmd}, cwd=wt, env=env)
        assert d.allow, f"must be allowed via evaluate(): {d.reason}"


def test_an_executable_bit_is_still_required_and_a_later_path_entry_is_used(tmp_path):
    """The `os.access(X_OK)` filter this module keeps (mirroring
    `shutil.which`'s own filter, not its swallow) must still reject a
    readable-but-not-executable candidate and keep walking to a later,
    executable one — otherwise a stray non-executable file named `pip`
    earlier on `PATH` would shadow the real installer."""
    bindir1 = tmp_path / "d1" / "bin"
    bindir2 = tmp_path / "d2" / "bin"
    bindir1.mkdir(parents=True)
    bindir2.mkdir(parents=True)
    not_exec = bindir1 / "pip"
    not_exec.write_text("not actually executable\n")
    os.chmod(not_exec, 0o600)
    real_pip = bindir2 / "pip"
    real_pip.write_text("#!/bin/sh\nexit 0\n")
    st = os.stat(real_pip)
    os.chmod(real_pip, st.st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

    env = {"PATH": f"{bindir1}{os.pathsep}{bindir2}"}
    resolved = venv_install_guard._resolve_installer("pip", str(tmp_path), env)
    assert resolved == os.path.realpath(str(real_pip)), (
        "a non-executable earlier candidate must be skipped for a later executable one"
    )


def test_pathext_expansion_resolves_a_windows_shaped_bare_token(tmp_path, monkeypatch):
    """Blocker 2: abandoning `shutil.which` also dropped its `PATHEXT`
    expansion — on Windows, a bare `pip` on `PATH` names `pip.exe` on disk,
    not a file literally called `pip`. Without expanding `PATHEXT` the same
    way `shutil.which` does, the bare-token spelling this module's own
    comments call "the spelling a coder actually types" would stop
    resolving on Windows and silently fall through to allow-and-log —
    a DENY→ALLOW regression on the platform this guard must also cover.

    `_IS_WINDOWS` is monkeypatched directly (the suffix computation is the
    only platform-gated branch; the filesystem calls underneath are ordinary
    OS calls against real files this test creates) so the PATHEXT expansion
    path is exercised on whatever host runs the suite."""
    monkeypatch.setattr(venv_install_guard, "_IS_WINDOWS", True)
    bindir = tmp_path / "bin"
    bindir.mkdir()
    exe = bindir / "pip.EXE"
    exe.write_text("not a real PE, just needs to exist\n")
    os.chmod(exe, 0o755)

    # Split with `os.pathsep`, same as the code under test (and cpython's own
    # `shutil.which`) — on real Windows that is `;`; on this (POSIX) test
    # host it is `:`, and using anything else here would desync the test
    # from what the code actually parses without that being a real bug.
    env = {"PATH": str(bindir), "PATHEXT": os.pathsep.join([".COM", ".EXE", ".BAT"])}
    resolved = venv_install_guard._resolve_installer("pip", str(tmp_path), env)
    assert resolved == os.path.realpath(str(exe)), (
        f"must expand PATHEXT to find pip.EXE, got {resolved!r}"
    )

    # Negative control: without PATHEXT expansion (non-Windows), the literal
    # `pip` name is not on PATH and must resolve to nothing (allow-and-log),
    # proving the assertion above is really about PATHEXT, not some other
    # fallback.
    monkeypatch.setattr(venv_install_guard, "_IS_WINDOWS", False)
    resolved = venv_install_guard._resolve_installer("pip", str(tmp_path), env)
    assert resolved is None


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


# ---------------------------------------------------------------------------
# `shutil.which` parity for the hand-walked PATH scan. This module deliberately
# does NOT call `shutil.which` (it swallows `PermissionError`), so every OTHER
# resolution rule `which` implements has to be reproduced here by hand — and
# each one that is not is a DENY->ALLOW, because an installer this scan fails
# to resolve falls through to the module's one allow-and-log fallback.
# ---------------------------------------------------------------------------

def _mkwinvenv(root):
    """A Windows-shaped venv: the only `pip` on disk is `pip.EXE`, so the scan
    resolves it ONLY if it expands `PATHEXT` the way `shutil.which` does."""
    venv = os.path.join(root, ".venv")
    bindir = os.path.join(venv, "bin")
    os.makedirs(bindir, exist_ok=True)
    with open(os.path.join(venv, "pyvenv.cfg"), "w") as f:
        f.write("home = /usr/bin\n")
    for name in ("python.EXE", "pip.EXE"):
        path = os.path.join(bindir, name)
        with open(path, "w") as f:
            f.write("#!/bin/sh\nexit 0\n")
        st = os.stat(path)
        os.chmod(path, st.st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    with open(os.path.join(root, "pyproject.toml"), "w") as f:
        f.write("[project]\nname = \"x\"\n")
    return os.path.realpath(root), os.path.realpath(venv)


def test_default_pathext_constant_matches_the_stdlibs_own_value():
    """The fallback list is a copy of `shutil._WIN_DEFAULT_PATHEXT`; pin it so
    it cannot drift from the resolver this module claims parity with. Skipped
    rather than guessed if the stdlib ever drops the private name."""
    import shutil as _shutil
    stdlib = getattr(_shutil, "_WIN_DEFAULT_PATHEXT", None)
    if stdlib is None:  # pragma: no cover - stdlib-version dependent
        pytest.skip("this cpython has no shutil._WIN_DEFAULT_PATHEXT to pin against")
    assert venv_install_guard._WIN_DEFAULT_PATHEXT == tuple(stdlib.split(";"))


@pytest.mark.parametrize(
    "pathext,label",
    [
        (None, "PATHEXT absent from env AND from os.environ"),
        ("", "PATHEXT set to the empty string"),
        (os.pathsep.join([".COM", ".EXE", ".BAT"]) + os.pathsep, "trailing separator"),
        (os.pathsep.join([".COM", ".EXE.", ".BAT"]), "trailing dot on an entry"),
    ],
)
def test_pathext_readings_that_shutil_which_resolves_still_deny(
    tmp_path, monkeypatch, pathext, label
):
    """`shutil.which`'s win32 branch reads `PATHEXT` as
    `os.getenv("PATHEXT") or _WIN_DEFAULT_PATHEXT`, then
    `[ext.rstrip('.') for ext in ... if ext]`. Three consequences this scan
    must share, or a bare `pip install` on Windows resolves to nothing and is
    ALLOWED into the shared venv:

    * unset OR EMPTY `PATHEXT` falls back to the DEFAULT list — it does not
      mean "no expansion" (`cmd.exe` still runs `pip.EXE`);
    * an empty entry, which a trailing `;` produces and real Windows
      `PATHEXT` values commonly carry, is DROPPED. Keeping it makes
      `token.endswith("")` true for EVERY token, which flips the candidate
      ORDER to bare-token-first for everything and produces a false DENY —
      see `test_a_trailing_separator_does_not_flip_the_candidate_order`;
    * a trailing dot on an entry is stripped.

    `_IS_WINDOWS` is monkeypatched, exactly as the neighbouring PATHEXT test
    does: the suffix computation is the only platform-gated branch and the
    filesystem calls under it are ordinary OS calls on files this test
    creates."""
    monkeypatch.setattr(venv_install_guard, "_IS_WINDOWS", True)
    monkeypatch.delenv("PATHEXT", raising=False)
    primary, primary_venv = _mkwinvenv(str(tmp_path / "primary"))
    wt, wt_venv = _mkwinvenv(str(tmp_path / "wt"))
    env = {"PATH": os.path.join(primary_venv, "bin"), "VIRTUAL_ENV": primary_venv}
    if pathext is not None:
        env["PATHEXT"] = pathext

    resolved = venv_install_guard._resolve_installer("pip", wt, env)
    assert resolved == os.path.join(primary_venv, "bin", "pip.EXE"), (
        f"{label}: the scan must expand PATHEXT the way shutil.which does; "
        f"got {resolved!r}"
    )
    r = venv_install_guard.denial_reason("pip install evilpkg", cwd=wt, env=env)
    assert r is not None, (
        f"{label}: an install into the shared venv outside {wt} must be "
        "DENIED, not allowed-and-logged because PATHEXT failed to expand"
    )
    assert primary_venv in r
    d = _ev("Bash", {"command": "pip install evilpkg"}, cwd=wt, env=env)
    assert not d.allow, f"{label}: must be denied via evaluate(): {d.reason}"


def test_pathext_negative_control_a_genuinely_absent_installer_is_not_invented(
    tmp_path, monkeypatch
):
    """Positive control for the parametrized test above: the same fixture with
    NO `pip.EXE` on PATH must resolve to nothing, proving those assertions are
    about PATHEXT expansion finding a real file and not about the scan
    accepting any candidate it forms."""
    monkeypatch.setattr(venv_install_guard, "_IS_WINDOWS", True)
    monkeypatch.delenv("PATHEXT", raising=False)
    wt, wt_venv = _mkwinvenv(str(tmp_path / "wt"))
    empty_bin = tmp_path / "empty-bin"
    empty_bin.mkdir()
    env = {"PATH": str(empty_bin)}
    assert venv_install_guard._resolve_installer("pip", wt, env) is None


# ---------------------------------------------------------------------------
# The venv guard's resolver never resolves a Windows candidate path.
#
# `_safe_realpath` (`os.path.realpath`) returns a NATIVE-separator path on a
# real Windows host by construction — backslash only, never through
# `win_readings.readings` (that helper normalises a raw COMMAND TOKEN's
# spelling; it neither sees nor can see a `realpath` RETURN value). `_basename`
# is deliberately `PurePosixPath`-only (see its docstring), so handed a
# `realpath` return of `C:\venv\Scripts\uv.EXE` it reads the whole string as
# one opaque component and never recognises `uv`. Every one of the FOUR call
# sites that inspects a resolved path used `_basename` for this — so a real
# Windows install, having just been stat'd and confirmed to exist, fell
# through to this module's one allow-and-log fallback (a fail-OPEN) instead of
# being recognised and (correctly) denied. `_resolved_basename` fixes this by
# re-reading `/`-normalised, but ONLY when `_IS_WINDOWS`; `_basename` itself,
# and everywhere it is still called on a raw COMMAND TOKEN, is unchanged.
#
# The tests below simulate the Windows behaviour on a POSIX test host by
# monkeypatching `_safe_realpath`'s return spelling (and, where the branch
# under test reads that return value directly, `_probe_is_file`'s input
# spelling) — nothing else. A POSIX host's own `os.path.realpath` cannot
# itself produce a backslash path, so this is the only way to reproduce the
# defect without a real Windows machine; see the task's own open question
# about a real-Windows confirmatory run, which this suite cannot answer.
#
# `_venv_root_of` (out of scope for this fix) calls the plain, POSIX-flavoured
# `os.path.dirname`/`os.path.join` unconditionally — correct on a real Windows
# host, where `os.path` IS `ntpath` and splits on `\` natively, but NOT
# reproducible by flipping a string's separators on a POSIX test host, where
# `os.path` stays `posixpath` regardless of `_IS_WINDOWS`. `posixpath.dirname`
# on a fully backslashed string (no `/` at all) returns `""` unconditionally —
# confirmed directly (`os.path.dirname('/x'.replace('/', chr(92)))` is
# `''`) — so `_venv_root_of` returns `None` for ANY native-separator path on
# this test host, independent of whether this ticket's fix is applied. Tests
# below therefore assert what IS mutation-provable on POSIX — that
# `_resolve_installer` and `_effective_prefixes` themselves resolve/exclude
# correctly — rather than a full `denial_reason` DENY that would silently
# pass or fail by accident of `_venv_root_of`'s host-independent dirname use,
# not by the fix under test.
# ---------------------------------------------------------------------------


def _native_sep_realpath(monkeypatch):
    """Make `_safe_realpath` behave like a real Windows host's: the same
    resolution, spelled with `\\` instead of `/`. Also carries `_probe_is_file`
    through the matching translation, because a real Windows `os.stat`
    accepts a backslash path natively where POSIX `os.stat` does not — a
    branch that probes the (now backslashed) `real` value directly needs this
    half of the simulation too, or the probe reports a path that was never
    created as absent. Idempotent on an already-forward-slash path, so it is
    safe to install even in a test whose PATH-walk probes an unshimmed,
    forward-slash `candidate`."""
    real_realpath = venv_install_guard._safe_realpath
    real_probe = venv_install_guard._probe_is_file

    def fake_realpath(path):
        r = real_realpath(path)
        return r.replace("/", "\\") if r else r

    def fake_probe(path):
        return real_probe(path.replace("\\", "/"))

    monkeypatch.setattr(venv_install_guard, "_safe_realpath", fake_realpath)
    monkeypatch.setattr(venv_install_guard, "_probe_is_file", fake_probe)


def test_the_windows_fixture_denies_a_known_bad_row_first(tmp_path):
    """AC5 fixture-validity control: before trusting any ALLOW this suite
    measures under the fix, confirm the test corpus CAN still produce a DENY
    at all. Plain POSIX `_session` fixture, no Windows simulation — this is
    the same shape `test_control_production_env_uv_commands_stay_allowed`'s
    neighbours use to prove a DENY fires for a real cross-venv install, so a
    later ALLOW in this section is a genuine fix effect and not a fixture
    that rubber-stamps everything."""
    primary, primary_venv, wt, wt_venv, prod_env, wt_env = _session(tmp_path)
    r = venv_install_guard.denial_reason("pip install evilpkg", cwd=wt, env=prod_env)
    assert r is not None, "fixture-validity control: this corpus must be able to deny something"
    assert primary_venv in r


def test_a_native_separator_realpath_still_resolves_the_explicit_path(tmp_path, monkeypatch):
    """AC1 / AC3, explicit-path branch (`venv_install_guard.py`): an
    explicit-path command token (`token` contains `/` — already
    `/`-normalised by `win_readings.readings` upstream, per `_basename`'s own
    docstring) whose `realpath` comes back native-separator must still
    resolve, not fall through to the allow-and-log fallback. This branch also
    calls `_probe_is_file` directly on the (now backslashed) `real` value, so
    the simulation needs `_probe_is_file`'s half of `_native_sep_realpath`,
    not just `_safe_realpath`'s."""
    monkeypatch.setattr(venv_install_guard, "_IS_WINDOWS", True)
    _native_sep_realpath(monkeypatch)
    primary, primary_venv = _mkwinvenv(str(tmp_path / "primary"))
    wt, wt_venv = _mkwinvenv(str(tmp_path / "wt"))
    real_pip = os.path.join(primary_venv, "bin", "pip.EXE")
    token = os.path.relpath(real_pip, wt)
    assert "/" in token  # sanity: must land in the explicit-path branch, not the PATH walk

    resolved = venv_install_guard._resolve_installer(token, wt, {})
    expected = real_pip.replace("/", "\\")
    assert resolved == expected, (
        f"a native-separator realpath must still resolve an explicit-path "
        f"token to the installer it actually is; got {resolved!r}, expected {expected!r}"
    )

    # Negative control: a token that resolves nowhere real stays None, so the
    # positive assertion above is not trivially true for any string.
    missing_token = os.path.relpath(os.path.join(primary_venv, "bin", "ghost.EXE"), wt)
    assert venv_install_guard._resolve_installer(missing_token, wt, {}) is None


def test_a_native_separator_realpath_still_resolves_the_bare_token(tmp_path, monkeypatch):
    """AC1 / AC3, PATH-walk branch (`venv_install_guard.py`, the
    determinate-probe return): the same defect reached by a bare
    `pip install ...` instead of an explicit path. `_probe_is_file`/
    `os.access` in this branch run against the UNSHIMMED `candidate` built
    from a `PATH` entry (never `realpath` output, so never backslashed) —
    only `_safe_realpath`'s return needs the native-separator simulation
    here, unlike the explicit-path branch above."""
    monkeypatch.setattr(venv_install_guard, "_IS_WINDOWS", True)
    _native_sep_realpath(monkeypatch)
    primary, primary_venv = _mkwinvenv(str(tmp_path / "primary"))
    wt, wt_venv = _mkwinvenv(str(tmp_path / "wt"))
    env = {"PATH": os.path.join(primary_venv, "bin")}

    resolved = venv_install_guard._resolve_installer("pip", wt, env)
    expected = os.path.join(primary_venv, "bin", "pip.EXE").replace("/", "\\")
    assert resolved == expected, (
        f"a native-separator realpath must still resolve the bare-token PATH "
        f"walk; got {resolved!r}, expected {expected!r}"
    )

    # Negative control: an empty PATH directory invents no resolution.
    empty_bin = tmp_path / "empty-bin"
    empty_bin.mkdir()
    assert venv_install_guard._resolve_installer("pip", wt, {"PATH": str(empty_bin)}) is None


@requires_chmod
def test_an_undetermined_native_separator_candidate_is_still_remembered(
    tmp_path, monkeypatch, caplog
):
    """AC1 / AC3, the fail-closed undetermined-probe path
    (`venv_install_guard.py`): a candidate whose file type cannot be
    verified (a `chmod` on an ancestor directory makes even `os.stat` raise
    `PermissionError`) must still be RECOGNISED as an installer from its
    native-separator `realpath` spelling and REMEMBERED as the resolved
    candidate — not silently dropped, which would fail this branch open the
    same way the determinate branches did, just reached via `probe is None`
    instead of `probe is True`."""
    monkeypatch.setattr(venv_install_guard, "_IS_WINDOWS", True)
    _native_sep_realpath(monkeypatch)
    primary, primary_venv = _mkwinvenv(str(tmp_path / "primary"))
    wt, wt_venv = _mkwinvenv(str(tmp_path / "wt"))
    env = {"PATH": os.path.join(primary_venv, "bin")}

    expected = os.path.join(primary_venv, "bin", "pip.EXE").replace("/", "\\")
    with _unreadable(primary_venv):
        caplog.clear()
        with caplog.at_level(logging.WARNING, logger=venv_install_guard._LOG.name):
            resolved = venv_install_guard._resolve_installer("pip", wt, env)
        assert resolved == expected, (
            f"an undetermined native-separator candidate must still be "
            f"remembered and returned, not dropped; got {resolved!r}"
        )
        assert any("could not be verified" in r.getMessage() for r in caplog.records), (
            "the fail-closed remembered-candidate path must log its WARNING"
        )


def test_a_native_separator_uv_is_still_excluded_from_prefixes(monkeypatch):
    """AC1 / AC3, collateral correctness (`venv_install_guard.py`): `uv`/
    `uvx` are deliberately excluded from `_effective_prefixes`'s
    owning-venv candidates (a resolved `uv` commonly lives inside the shared
    dev venv itself; treating its own directory as a candidate would DENY an
    ordinary `uv sync`/`uv run`). That exclusion is also a `_basename` call on
    a resolved path, so it shares this same bug: a native-separator `uv.exe`
    must still be recognised as `uv` and excluded, or this fix reintroduces a
    FALSE DENY on Windows for the exact commands the earlier
    `test_control_production_env_uv_commands_stay_allowed`-style tests pin as
    ALLOW on POSIX.

    `_venv_root_of` is monkeypatched to a fixed sentinel here (see this
    section's header comment: it cannot be driven by a native-separator path
    on a POSIX test host regardless of this fix) purely so the exclusion's
    effect is OBSERVABLE — mutation-sensitive — in the returned candidate
    set: with the fix, the exclusion's `continue` fires and `_venv_root_of`
    is never reached for this token; reverted, it is, and the sentinel leaks
    into `candidates`."""
    monkeypatch.setattr(venv_install_guard, "_IS_WINDOWS", True)
    sentinel_root = "C:\\p\\.venv"
    monkeypatch.setattr(venv_install_guard, "_venv_root_of", lambda exe: sentinel_root)
    exe = "C:\\p\\.venv\\Scripts\\uv.exe"

    candidates = venv_install_guard._effective_prefixes(
        tokens=["uv", "sync"], cwd=None, installers=[exe], env=None, uses_active=False
    )
    assert sentinel_root not in candidates, (
        "a native-separator uv/uvx installer must still be excluded from the "
        "owning-venv candidate set, or an ordinary `uv sync`/`uv run` earns a "
        "fresh false DENY on Windows"
    )


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("pip", "pip"),
        ("pip.exe", "pip"),
        ("PIP.EXE", "PIP"),
        ("/usr/bin/pip3", "pip3"),
        ("/bin/sh/", "sh"),
        ("C:\\venv\\Scripts\\uv.EXE", "C:\\venv\\Scripts\\uv"),
    ],
)
def test_basename_posix_reading_is_unchanged(raw, expected):
    """AC2 control: `_basename` itself is OUT OF SCOPE for this fix and its
    body must be byte-for-byte unchanged — pinned directly against its own
    documented cases (trailing separator, host independence) plus the exact
    native-separator string this ticket is about, which `_basename` ALONE
    still reads as one opaque component. Splitting it is `_resolved_basename`'s
    job, exercised separately below and at the call sites above; this table
    is the control proving `_basename`'s own reading did not move."""
    assert venv_install_guard._basename(raw) == expected


def test_resolved_basename_is_a_no_op_on_posix(tmp_path, monkeypatch):
    """AC2: `_resolved_basename` is gated on `_IS_WINDOWS`, so on a POSIX host
    it must be a structural no-op — identical to `_basename` for any input,
    including a string that LOOKS like a native Windows path, because `\\` is
    a legal POSIX filename character and normalising it unconditionally would
    let a file genuinely named `a\\pip` start reading as `pip`.

    The end-to-end row makes the same point through the resolver: a real file
    on `PATH`, reached via a symlink, whose target is literally named
    `foo\\uv` (legal on POSIX) must NOT be recognised as `uv` on a POSIX
    host — `_resolved_basename` must not split off the `foo\\` prefix here,
    unlike it would (correctly) on Windows."""
    monkeypatch.setattr(venv_install_guard, "_IS_WINDOWS", False)
    for raw in ("a\\pip", "C:\\venv\\Scripts\\uv.EXE", "/usr/bin/uv", "uv.exe", "pip3"):
        assert venv_install_guard._resolved_basename(raw) == venv_install_guard._basename(raw)

    bindir = tmp_path / "bin"
    bindir.mkdir()
    target = bindir / "foo\\uv"
    target.write_text("#!/bin/sh\nexit 0\n")
    st = os.stat(target)
    os.chmod(target, st.st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    link = bindir / "uv"
    link.symlink_to(target)

    resolved = venv_install_guard._resolve_installer("uv", str(tmp_path), {"PATH": str(bindir)})
    assert resolved is None, (
        "a POSIX file literally named foo\\uv, reached via a uv symlink, must "
        "not be misread as a bare uv on a POSIX host"
    )


def test_an_empty_path_entry_is_the_current_directory(tmp_path):
    """An empty `PATH` entry means THE CURRENT DIRECTORY — POSIX ("a
    zero-length prefix ... indicates the current working directory") and
    cpython's `shutil.which`, whose source says so outright ("PATH='' doesn't
    match, whereas PATH=':' looks in the current directory") and implements it
    by leaving `os.path.join("", thefile)` relative.

    Skipping that entry made `PATH=:<dir>` plus a `pip` in the session's own
    cwd — a laundering a coder can set up with one symlink — resolve to
    NOTHING and fall through to allow-and-log, while the shell the command is
    actually handed to runs that `pip` and writes into the shared venv."""
    primary, primary_venv, wt, wt_venv, prod_env, wt_env = _session(tmp_path)
    os.symlink(os.path.join(primary_venv, "bin", "pip"), os.path.join(wt, "pip"))
    empty_bin = tmp_path / "empty-bin"
    empty_bin.mkdir()
    env = {"PATH": "" + os.pathsep + str(empty_bin), "VIRTUAL_ENV": primary_venv}

    # What the real resolver does with the same PATH, from the same cwd.
    old = os.getcwd()
    os.chdir(wt)
    try:
        assert shutil.which("pip", path=env["PATH"]) is not None, (
            "premise: cpython's own resolver finds ./pip through the empty entry"
        )
    finally:
        os.chdir(old)

    resolved = venv_install_guard._resolve_installer("pip", wt, env)
    assert resolved == os.path.join(primary_venv, "bin", "pip"), (
        f"the empty PATH entry must resolve against the command's cwd; got {resolved!r}"
    )
    r = venv_install_guard.denial_reason("pip install evilpkg", cwd=wt, env=env)
    assert r is not None and primary_venv in r, (
        f"must DENY an install that resolves into {primary_venv}: {r!r}"
    )
    d = _ev("Bash", {"command": "pip install evilpkg"}, cwd=wt, env=env)
    assert not d.allow, f"must be denied via evaluate(): {d.reason}"


def test_an_empty_path_entry_does_not_invent_a_deny(tmp_path):
    """Positive control for the test above, and the property that keeps the
    reading from becoming a blanket DENY: the identical PATH with NO `pip` in
    the cwd resolves to nothing, and a `pip` in the cwd that belongs to the
    session's OWN worktree venv stays ALLOWED."""
    primary, primary_venv, wt, wt_venv, prod_env, wt_env = _session(tmp_path)
    empty_bin = tmp_path / "empty-bin"
    empty_bin.mkdir()
    env = {"PATH": "" + os.pathsep + str(empty_bin)}
    assert venv_install_guard._resolve_installer("pip", wt, env) is None, (
        "nothing named pip in the cwd -> nothing to resolve"
    )
    # ...and the session's own venv, reached through the same empty entry.
    os.symlink(os.path.join(wt_venv, "bin", "pip"), os.path.join(wt, "pip"))
    assert venv_install_guard._resolve_installer("pip", wt, env) == os.path.join(
        wt_venv, "bin", "pip"
    )
    assert venv_install_guard.denial_reason("pip install foo", cwd=wt, env=env) is None, (
        "installing into the session's OWN worktree venv must stay allowed"
    )


def test_windows_searches_the_current_directory_like_cmd_exe(tmp_path, monkeypatch):
    """Same class as the empty-entry reading, one platform over: on win32
    `shutil.which` prepends the current directory (`_win_path_needs_curdir` ->
    `NeedCurrentDirectoryForExePath`, true unless
    `NoDefaultCurrentDirectoryInExePath` is set) because that is what
    `cmd.exe` searches. A `pip.EXE` dropped in the session's cwd that points
    into the shared venv must therefore be DENIED, not allowed-and-logged."""
    monkeypatch.setattr(venv_install_guard, "_IS_WINDOWS", True)
    monkeypatch.delenv("PATHEXT", raising=False)
    primary, primary_venv = _mkwinvenv(str(tmp_path / "primary"))
    wt, wt_venv = _mkwinvenv(str(tmp_path / "wt"))
    os.symlink(os.path.join(primary_venv, "bin", "pip.EXE"),
               os.path.join(wt, "pip.EXE"))
    empty_bin = tmp_path / "empty-bin"
    empty_bin.mkdir()
    env = {"PATH": str(empty_bin), "VIRTUAL_ENV": primary_venv}

    resolved = venv_install_guard._resolve_installer("pip", wt, env)
    assert resolved == os.path.join(primary_venv, "bin", "pip.EXE"), (
        f"the cwd must be searched on Windows, as cmd.exe does; got {resolved!r}"
    )
    r = venv_install_guard.denial_reason("pip install evilpkg", cwd=wt, env=env)
    assert r is not None and primary_venv in r, f"must DENY: {r!r}"

    # Negative control: the identical fixture on POSIX, where the cwd is NOT
    # on the search path, resolves to nothing — proving the assertion above is
    # about the win32 curdir rule and not about some other fallback.
    monkeypatch.setattr(venv_install_guard, "_IS_WINDOWS", False)
    assert venv_install_guard._resolve_installer("pip", wt, env) is None


def _win_pathdir(tmp_path, monkeypatch, names_to_venv):
    """One PATH directory whose entries point into DIFFERENT venvs.

    `names_to_venv` maps a filename on PATH to either the OWN venv (under the
    session's cwd) or the FOREIGN one (outside it). Containment under `cwd` is
    what makes a venv "own" — naming a directory does not.
    """
    monkeypatch.setattr(venv_install_guard, "_IS_WINDOWS", True)
    wt, wt_venv = _mkwinvenv(str(tmp_path / "wt"))
    foreign, foreign_venv = _mkwinvenv(str(tmp_path / "primary"))
    pathdir = os.path.join(str(tmp_path), "pathdir")
    os.makedirs(pathdir, exist_ok=True)
    for name, which in names_to_venv.items():
        target = os.path.join(
            wt_venv if which == "own" else foreign_venv, "bin", "pip.EXE")
        os.symlink(target, os.path.join(pathdir, name))
    # `_resolve_installer` splits PATHEXT on `os.pathsep`, which is ":" on the
    # POSIX hosts that run this suite with `_IS_WINDOWS` monkeypatched. Writing
    # the Windows ";" spelling here leaves the whole value as ONE bogus
    # extension and the test silently measures nothing.
    env = {"PATH": pathdir,
           "PATHEXT": os.pathsep.join((".COM", ".EXE", ".BAT")),
           "VIRTUAL_ENV": foreign_venv}
    return wt, wt_venv, foreign_venv, env


def test_the_pathext_candidate_ORDER_matches_which_in_both_of_its_cases(
    tmp_path, monkeypatch
):
    """`shutil.which`'s win32 order is CONDITIONAL and no fixed order matches it.

    `which` builds `[cmd + ext for ext in pathext]` and inserts the BARE token
    at the FRONT only when the token already ends with a PATHEXT entry. So:

    * bare-token-first is wrong for a plain `pip` — it resolves an
      extensionless decoy ahead of the `pip.EXE` the shell would run;
    * bare-token-last is wrong for `pip.exe` — `which` prefers the bare token
      there, and appending it lets `pip.exe.COM` win instead.

    Each fixed order produces a FALSE DENY in the case the other gets right;
    both were measured before this test existed. This pins the condition, and
    it is the only thing that does — reverting the order left every other test
    green.
    """
    # Case 1: plain token. `which` runs pip.EXE (own) -> must ALLOW.
    wt, wt_venv, _, env = _win_pathdir(
        tmp_path / "a", monkeypatch, {"pip": "foreign", "pip.EXE": "own"})
    got = venv_install_guard._resolve_installer("pip", wt, env)
    assert got == os.path.join(wt_venv, "bin", "pip.EXE"), (
        "a bare token must not resolve an extensionless decoy ahead of the "
        f"pip.EXE the shell would run; got {got!r}")

    # Case 2: the mirror. `which` runs pip.EXE (foreign) -> must DENY.
    wt2, _, foreign2, env2 = _win_pathdir(
        tmp_path / "b", monkeypatch, {"pip": "own", "pip.EXE": "foreign"})
    got2 = venv_install_guard._resolve_installer("pip", wt2, env2)
    assert got2 == os.path.join(foreign2, "bin", "pip.EXE"), (
        f"the foreign pip.EXE is what the shell runs; got {got2!r}")

    # Case 3: token ALREADY carries an extension. `which` puts the bare token
    # FIRST here, so pip.exe (own) must win over pip.exe.COM (foreign).
    wt3, wt3_venv, _, env3 = _win_pathdir(
        tmp_path / "c", monkeypatch,
        {"pip.exe": "own", "pip.exe.COM": "foreign"})
    got3 = venv_install_guard._resolve_installer("pip.exe", wt3, env3)
    assert got3 == os.path.join(wt3_venv, "bin", "pip.EXE"), (
        "an already-extended token prefers the bare spelling, as which does; "
        f"got {got3!r}")


def test_a_trailing_separator_does_not_flip_the_candidate_order(
    tmp_path, monkeypatch
):
    """`.COM;.EXE;.BAT;` is an ordinary Windows `PATHEXT`, and the empty entry
    it produces must not reach the ORDER condition.

    That condition asks whether the token already ends with a PATHEXT entry,
    and `"PIP".endswith("")` is true for EVERY token. So a surviving empty
    entry flips every token to bare-token-first and reinstates exactly the
    false DENY the conditional order exists to prevent. The `if ext` filter in
    `_resolve_installer` is what stops that, and before this test nothing
    measured it: replacing the filter with `if True` left the whole suite
    green while resolving the FOREIGN venv here.
    """
    wt, wt_venv, foreign_venv, env = _win_pathdir(
        tmp_path, monkeypatch, {"pip": "foreign", "pip.EXE": "own"})
    env["PATHEXT"] = os.pathsep.join((".COM", ".EXE", ".BAT")) + os.pathsep

    got = venv_install_guard._resolve_installer("pip", wt, env)
    assert got == os.path.join(wt_venv, "bin", "pip.EXE"), (
        "a trailing PATHEXT separator must not flip the candidate order; "
        f"the shell runs pip.EXE (own venv), got {got!r}")
    assert venv_install_guard.denial_reason(
        "pip install requests", cwd=wt, env=env) is None, (
        "resolving the session's OWN venv must not be denied")


@requires_chmod
def test_a_denial_resting_on_an_unreadable_entry_does_not_claim_it_resolves_there(
    tmp_path,
):
    """#338: the verdict was right through a sentence that was not.

    When the determinate winner owns no venv, this module prefers the
    earlier entry it could not stat -- fail-closed on a tri-state read, and
    correct. But a POSIX shell SKIPS an EACCES entry and keeps walking, so
    the binary that denial named is one the shell would never execute, and
    the message told the user the install "resolves to" it. That is a false
    fact to act on, in a module whose thesis is "resolve what actually
    executes and where it writes".

    The verdict does not move -- both readings DENY -- so the control here
    is the READABLE one: it must still say "resolves to", because there the
    claim is established.
    """
    primary, primary_venv, wt, wt_venv, prod_env, wt_env = _session(tmp_path)
    sysbin = tmp_path / "sysbin"
    sysbin.mkdir()
    system_pip = sysbin / "pip"
    system_pip.write_text("#!/bin/sh\nexit 0\n")
    os.chmod(system_pip, 0o755)
    assert venv_install_guard._venv_root_of(str(system_pip)) is None

    env = {
        "PATH": f"{primary_venv}/bin{os.pathsep}{sysbin}",
        "VIRTUAL_ENV": primary_venv,
    }
    cmd = "pip install evilpkg"

    readable = venv_install_guard.denial_reason(cmd, cwd=wt, env=env)
    assert readable is not None
    assert "resolves to" in readable, (
        "CONTROL: a determinate read established the target, so the wording "
        f"that states it must survive; got {readable!r}"
    )

    with _unreadable(primary_venv):
        blocked = venv_install_guard.denial_reason(cmd, cwd=wt, env=env)
        assert blocked is not None, "the verdict must not move: still DENY"
        assert "resolves to" not in blocked, (
            "a denial resting on an entry that could not be stat'd must not "
            f"assert a resolution the shell would not make; got {blocked!r}"
        )
        assert "could not be read" in blocked
        # Still names the path, so the user can act on it -- as the thing
        # that could not be read, which is what is true.
        assert primary_venv in blocked
        assert wt in blocked, "the worktree it should have targeted instead"


@requires_chmod
def test_the_honest_wording_does_not_leak_into_a_fully_readable_denial(tmp_path):
    """The mirror of the control above, one layer in: an unreadable entry
    somewhere on PATH must not re-word a denial that does not rest on it.

    Here the unreadable entry is a determinate NON-venv, so the resolver
    keeps the determinate foreign-venv winner and the target IS established.
    """
    primary, primary_venv, wt, wt_venv, prod_env, wt_env = _session(tmp_path)
    opaque = tmp_path / "opaque"
    opaque.mkdir()
    (opaque / "unrelated").write_text("#!/bin/sh\nexit 0\n")
    os.chmod(opaque / "unrelated", 0o755)

    env = {
        "PATH": f"{opaque}{os.pathsep}{primary_venv}/bin",
        "VIRTUAL_ENV": primary_venv,
    }
    with _unreadable(opaque):
        blocked = venv_install_guard.denial_reason(
            "pip install evilpkg", cwd=wt, env=env)
        assert blocked is not None
        assert "resolves to" in blocked, (
            f"nothing this denial rests on was unreadable; got {blocked!r}"
        )


# ---------------------------------------------------------------------------
# AC1 — a capitalised installer spelling is refused wherever the lowercase
# spelling is refused. Issue: `_is_installer_name` only folded case when
# `_IS_WINDOWS`, so on a case-insensitive filesystem (macOS APFS by default)
# `PIP install evilpkg` was ALLOWED into the shared dev venv while
# `pip install evilpkg` was correctly DENIED — even though the shell resolves
# `PIP` to the very same `pip` binary there.
# ---------------------------------------------------------------------------

def test_a_capitalised_installer_is_refused_on_a_folding_cwd(tmp_path, monkeypatch):
    """With the probe pinned to `True` (a folding host), every capitalised
    spelling of an installer must be refused exactly where the lowercase
    spelling is — the measured `PIP install evilpkg -> ALLOW` bug closed."""
    monkeypatch.setattr(exec_names, "host_folds_case", lambda *a, **k: True)
    # `primary_extra_names` creates the literal `PIP`/`Pip`/`PIP3` files in
    # the shared venv's `bin/` -- mocking `host_folds_case` only changes the
    # CLASSIFICATION decision; on a genuinely case-sensitive test-runner
    # filesystem (Linux/ext4 CI) `_resolve_installer`'s real PATH walk still
    # needs a literal `"PIP"` file to exist on disk to resolve it the way a
    # real folding host's shell would for free.
    primary, primary_venv, wt, wt_venv, prod_env, wt_env = _session(
        tmp_path, primary_extra_names=("PIP", "Pip", "PIP3"))
    cases = [
        "pip install evilpkg",
        "PIP install evilpkg",
        "Pip install evilpkg",
        "PIP3 install evilpkg",
    ]
    # `UV add evilpkg` is deliberately NOT in this list: `uv` resolves its
    # target via `cwd`/`pyproject.toml`, never by matching its own basename
    # against an "owning venv" the way `pip`/`python` are checked here (see
    # the long comment above the `uv`/`uvx` exclusion in
    # `venv_install_guard.py`), so even the lowercase control (`uv add
    # evilpkg`) is not denied by this mechanism -- confirmed unchanged at
    # the pre-task baseline (680d6889). Case-consistency coverage for that
    # command lives in `test_a_capitalised_uv_commands_are_not_denied_like_pip`
    # instead, which asserts `uv add`/`UV add` match (both allowed).
    for cmd in cases:
        r = venv_install_guard.denial_reason(cmd, cwd=wt, env=prod_env)
        assert r is not None, f"must be denied on a folding host: {cmd}"
        d = _ev("Bash", {"command": cmd}, cwd=wt, env=prod_env)
        assert not d.allow, f"must be blocked via evaluate(): {cmd}"


def test_a_case_sensitive_host_still_allows_the_capitalised_spelling(
    tmp_path, monkeypatch
):
    """The #320 position, preserved: where `PIP` really is a different file
    from `pip` (a case-SENSITIVE volume), denying the capitalised spelling
    would refuse a command the user is entitled to run. This is also the
    control proving the fold — not some unrelated tightening — is what does
    the work in the test above."""
    monkeypatch.setattr(exec_names, "host_folds_case", lambda *a, **k: False)
    primary, primary_venv, wt, wt_venv, prod_env, wt_env = _session(tmp_path)

    lower = venv_install_guard.denial_reason(
        "pip install evilpkg", cwd=wt, env=prod_env)
    assert lower is not None, "the lowercase spelling must still be denied"

    upper = venv_install_guard.denial_reason(
        "PIP install evilpkg", cwd=wt, env=prod_env)
    assert upper is None, (
        "on a case-sensitive host PIP is a genuinely different program from "
        f"pip, and must not be refused: {upper}"
    )
    d = _ev("Bash", {"command": "PIP install evilpkg"}, cwd=wt, env=prod_env)
    assert d.allow, f"must stay allowed via evaluate(): {d.reason}"


def test_this_hosts_real_filesystem_answer_is_honoured(tmp_path):
    """No pinning: measure `tmp_path` for real and assert the `PIP` verdict
    tracks the measurement — the row that would have caught the live macOS
    bug with no mocks at all."""
    import os as _os

    written = tmp_path / "probe"
    written.write_text("x")
    other_spelling = tmp_path / "PROBE"
    folds_here = other_spelling.exists() and _os.path.samefile(
        other_spelling, written)

    primary, primary_venv, wt, wt_venv, prod_env, wt_env = _session(tmp_path)
    upper = venv_install_guard.denial_reason(
        "PIP install evilpkg", cwd=wt, env=prod_env)

    if folds_here:
        assert upper is not None, (
            "this volume folds case, so PIP resolves to the same program as "
            "pip, and the install must be denied"
        )
    else:
        assert upper is None, (
            "this volume does not fold case, so PIP is a distinct program "
            "from pip, and must not be refused"
        )
