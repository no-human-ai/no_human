"""Every text-decoding `subprocess` call in `src/no_human` names BOTH its
encoding and its error policy.

THE BUG (Windows-only, invisible on Linux CI, invisible on POSIX in general):
`subprocess.run(..., text=True)` with no explicit `encoding=` decodes a
child's pipes using the platform's preferred encoding — the ANSI code page on
Windows. When a byte the code page cannot represent shows up (any UTF-8
multi-byte character git or a Python child writes — a Hebrew commit subject,
a Cyrillic path, an emoji in a PR body), CPython's `Popen._communicate` reads
each pipe on a background THREAD (`Lib/subprocess.py::_readerthread`):

    def _readerthread(fh, buffer):
        buffer.append(fh.read())
        fh.close()

`fh` is a `TextIOWrapper` over the code page. `fh.read()` raises
`UnicodeDecodeError` INSIDE the thread; nothing there catches it, so the
thread dies silently and `buffer` never gets its append. `_communicate`'s own
tail then runs `stdout = stdout[0] if stdout else None` — the list is `[]`
(falsy), so this yields `None`, not a string. Every caller that goes on to
call `.strip()` or `.splitlines()` on that `None` gets an `AttributeError`
that names none of this; callers that guard `is None` instead silently
truncate the run. POSIX has no reader thread — `_communicate` decodes
inline and the same bad byte raises `UnicodeDecodeError` directly to the
caller, never `None` — which is why this bug is real on Windows and absent
in this repo's Linux CI.

THE FIX: `encoding="utf-8"` AND `errors="replace"`, together, at every call
that decodes text. Either alone is insufficient:

  * `encoding=` alone leaves `errors="strict"` (the `subprocess` default) —
    still raises inside the reader thread, still not closing the `None`
    class, just changing which bytes trigger it.
  * `errors=` alone (the pre-existing `core/review_routing.py` bug this
    module's tests cover) leaves the CODE PAGE as the codec. A code page
    practically never raises on `errors="replace"` alone (see
    `test_errors_alone_does_not_satisfy_the_guard`) — it just decodes with
    the wrong codec, producing silent mojibake instead of a crash. Quieter
    than `None`, and just as wrong.

Only both together closes the failure class without corrupting the common
case: valid UTF-8 (everything git and every UTF-8-locale Python child
emits) round-trips exactly, and only genuinely undecodable bytes become
U+FFFD.

SCOPE: only calls that already opted into TEXT MODE (`text=True`,
`universal_newlines=True`, or an `encoding=` already present) are guarded.
A `capture_output=True`-only call with none of those returns `bytes`; adding
`encoding=` there would flip its return type from `bytes` to `str`, a
behaviour change forbidden by this fix's own scope. Two such byte-mode sites
are pinned by name in `test_byte_mode_sites_are_left_alone` so a future edit
cannot silently widen this guard onto them.

`src/no_human/vcs/approve_merge.py` is deliberately NOT touched by the sweep
this module guards (see `test_approve_merge_sh_already_names_its_encoding`):
its one subprocess call site, `_sh` (line 233), already carries both
`encoding="utf-8"` and `errors="replace"` — landed ahead of this fix, and
also serves as this guard's POSITIVE CONTROL
(`test_the_guard_can_see_the_site_that_already_complies`): proof the scanner
below can see a compliant site and correctly NOT flag it, not merely that it
never flags anything.
"""
from __future__ import annotations

import ast
import io
import os
import pathlib
import subprocess
import sys
import threading

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = REPO_ROOT / "src" / "no_human"

#: `subprocess` functions that can decode text, mirroring the sweep's own
#: enumeration (`.no_human/PLAN.md`).
_SYNC_METHODS = frozenset({"run", "Popen", "check_output", "check_call", "call"})

#: A Hebrew commit-subject-shaped string used throughout this module. UTF-8
#: encodes its first letter (lamed, ל) as `D7 9C`; `0x9C` is undefined in
#: cp1255 (Hebrew Windows code page) — see
#: `test_the_subject_is_undecodable_as_cp1255` for the byte-level proof this
#: docstring only asserts in prose.
HEBREW_SUBJECT = "תיקון: הוספת טיפול בקידוד UTF-8"


# ---------------------------------------------------------------------------
# AC1: static guard over every text-decoding subprocess call in src/no_human
# ---------------------------------------------------------------------------

def _subprocess_aliases(tree: ast.Module) -> set[str]:
    """Every local name bound to the `subprocess` module in *tree* —
    `import subprocess` and `import subprocess as X` both, since three real
    sites in this repo (`config.py`, and two in `cli/commands.py`) use an
    alias."""
    aliases = {"subprocess"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            aliases |= {
                alias.asname or alias.name
                for alias in node.names
                if alias.name == "subprocess"
            }
    return aliases


def _subprocess_call_sites(tree: ast.Module, aliases: set[str]) -> list[ast.Call]:
    """Every `<alias>.run/.Popen/.check_output/.check_call/.call(...)` call
    in *tree*, regardless of whether it decodes text."""
    return [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in _SYNC_METHODS
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id in aliases
    ]


def _is_undeclared_decode_call(call: ast.Call, aliases: set[str]) -> bool:
    """Whether *call* decodes text without pinning BOTH `encoding=` and
    `errors=`.

    Byte-mode calls (no `text=`/`universal_newlines=`/`encoding=` at all)
    are out of scope by design — see the module docstring's SCOPE section —
    so they are never flagged here regardless of what else they pass.
    """
    if not (
        isinstance(call, ast.Call)
        and isinstance(call.func, ast.Attribute)
        and call.func.attr in _SYNC_METHODS
        and isinstance(call.func.value, ast.Name)
        and call.func.value.id in aliases
    ):
        return False
    kw = {k.arg for k in call.keywords}
    text_mode = bool(kw & {"text", "universal_newlines", "encoding"})
    if not text_mode:
        return False
    return not ({"encoding", "errors"} <= kw)


def _seam_param_names(fn: ast.AST, aliases: set[str]) -> set[str]:
    """Parameter names of *fn* whose DEFAULT VALUE is a bound subprocess
    method — the injectable-runner seam pattern
    (``def install(*, runner=subprocess.run): ... runner(cmd, text=True)``).
    The call site itself, ``runner(...)``, names no subprocess attribute at
    all: it is a bare ``ast.Name`` call, structurally invisible to
    `_subprocess_call_sites`'s `<alias>.<method>(...)` shape. This is
    exactly the class of site `walks_provision.install_walks` slipped
    through as before this scanner learned to resolve it: `runner` defaults
    to `subprocess.run` (the seam), and the call at its use site carries
    `text=True` with neither `encoding=` nor `errors=`.
    """
    if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return set()
    args = fn.args
    positional = args.posonlyargs + args.args
    defaults = args.defaults
    pairs = list(zip(positional[len(positional) - len(defaults):], defaults)) if defaults else []
    pairs += list(zip(args.kwonlyargs, args.kw_defaults))
    seams = set()
    for arg, default in pairs:
        if (
            isinstance(default, ast.Attribute)
            and isinstance(default.value, ast.Name)
            and default.value.id in aliases
            and default.attr in _SYNC_METHODS
        ):
            seams.add(arg.arg)
    return seams


def _is_undeclared_seam_call(call: ast.Call, seam_params: set[str]) -> bool:
    """Whether *call* is a bare-name call through an injected-runner seam
    (``runner(...)`` where ``runner`` is a parameter defaulting to a bound
    subprocess method) that decodes text without pinning both `encoding=`
    and `errors=`. Mirrors `_is_undeclared_decode_call`'s kwarg logic
    exactly — only the shape of the callee differs (`ast.Name` bound to a
    seam parameter, not `<alias>.<method>` attribute access)."""
    if not (isinstance(call.func, ast.Name) and call.func.id in seam_params):
        return False
    kw = {k.arg for k in call.keywords}
    text_mode = bool(kw & {"text", "universal_newlines", "encoding"})
    if not text_mode:
        return False
    return not ({"encoding", "errors"} <= kw)


def _seam_offenders(tree: ast.Module, aliases: set[str]) -> list[int]:
    """Line numbers of undeclared-decode calls made through an
    injected-runner seam anywhere in *tree* — the counterpart to
    `_is_undeclared_decode_call` for call sites a direct
    `<alias>.<method>(...)` walk cannot see. Scoped per function: a
    parameter only seams the body of the function that declares it."""
    offenders = []
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        seam_params = _seam_param_names(fn, aliases)
        if not seam_params:
            continue
        for node in ast.walk(fn):
            if isinstance(node, ast.Call) and _is_undeclared_seam_call(node, seam_params):
                offenders.append(node.lineno)
    return offenders


def _offending_lines(tree: ast.Module) -> list[int]:
    """Every undeclared-decode call line in *tree*: direct
    `<alias>.<method>(...)` sites AND injected-runner-seam sites."""
    aliases = _subprocess_aliases(tree)
    direct = [
        node.lineno for node in ast.walk(tree)
        if _is_undeclared_decode_call(node, aliases)
    ]
    return sorted(direct + _seam_offenders(tree, aliases))


def _offenders(path: pathlib.Path) -> list[int]:
    """Line numbers of undeclared-decode subprocess calls in *path*, or `[]`
    on a syntax error (mirrors `test_text_reads_declare_encoding.py`'s
    `_unencoded_read_text` — a file this scanner cannot parse can't be
    reasoned about, and every file under `src/no_human` does parse)."""
    try:
        tree = ast.parse(path.read_bytes(), filename=str(path))
    except SyntaxError:
        return []
    return _offending_lines(tree)


def test_no_subprocess_in_src_decodes_with_the_host_codepage():
    offenders = []
    for path in sorted(SRC.rglob("*.py")):
        rel = path.relative_to(REPO_ROOT).as_posix()
        for lineno in _offenders(path):
            offenders.append(f"{rel}:{lineno}")

    assert offenders == [], (
        "these subprocess calls decode text without pinning BOTH "
        'encoding="utf-8" and errors=<policy>, so on Windows an undecodable '
        "byte kills the reader thread silently and the caller gets None "
        "instead of a string (see this module's docstring): "
        + ", ".join(offenders)
    )


def test_the_guard_can_see_the_site_that_already_complies():
    """A guard that scores zero because its scanner can't see subprocess
    calls at all looks identical to a guard that scores zero because the
    tree is clean. `approve_merge.py`'s `_sh` (line 233) already carries
    both kwargs — the scanner must SEE this site and correctly not flag it,
    proving the zero above means something."""
    path = SRC / "vcs" / "approve_merge.py"
    tree = ast.parse(path.read_bytes(), filename=str(path))
    aliases = _subprocess_aliases(tree)
    sites = _subprocess_call_sites(tree, aliases)
    assert [n.lineno for n in sites] == [233], (
        "expected exactly one subprocess call site in approve_merge.py, at "
        "line 233 (_sh) — if this changed, the positive control needs "
        "updating alongside it"
    )
    assert _is_undeclared_decode_call(sites[0], aliases) is False
    assert _offenders(path) == []


@pytest.mark.parametrize(
    ("source", "flagged", "why"),
    [
        (b"import subprocess\nsubprocess.run(x, text=True)\n",
         True, "text=True with neither encoding= nor errors="),
        (b"import subprocess\nsubprocess.run(x, text=True, encoding='utf-8')\n",
         True, "encoding= without errors="),
        (b"import subprocess\nsubprocess.run(x, text=True, errors='replace')\n",
         True, "errors= without encoding= (the review_routing.py precedent)"),
        (b"import subprocess\n"
         b"subprocess.run(x, text=True, encoding='utf-8', errors='replace')\n",
         False, "both present"),
        (b"import subprocess\nsubprocess.run(x, capture_output=True)\n",
         False, "byte-mode: no text=/universal_newlines=/encoding= at all"),
        (b"import subprocess\nsubprocess.run(x, universal_newlines=True)\n",
         True, "universal_newlines is the same opt-in as text="),
        (b"import subprocess as sp\nsp.run(x, text=True)\n",
         True, "an aliased import must still be resolved"),
        (b"import subprocess\nsubprocess.Popen(x, text=True)\n",
         True, "Popen is covered, not just run"),
        (b"import subprocess\nsubprocess.check_output(x, text=True)\n",
         True, "check_output is covered"),
        (b"import subprocess\nsome_other_module.run(x, text=True)\n",
         False, "not a call through a resolved subprocess alias"),
    ],
)
def test_the_scanner_matches_decoding_calls_and_nothing_else(source, flagged, why):
    tree = ast.parse(source)
    aliases = _subprocess_aliases(tree)
    hits = [n for n in ast.walk(tree) if _is_undeclared_decode_call(n, aliases)]
    assert bool(hits) is flagged, why


@pytest.mark.parametrize(
    ("source", "flagged", "why"),
    [
        (b"import subprocess\n"
         b"def install(*, runner=subprocess.run):\n"
         b"    runner(x, capture_output=True, text=True)\n",
         True,
         "an injected-runner seam (default arg = subprocess.run) must be "
         "flagged, not just direct subprocess.*(...) calls — this is "
         "exactly walks_provision.install_walks's shape"),
        (b"import subprocess\n"
         b"def install(*, runner=subprocess.run):\n"
         b"    runner(x, capture_output=True, text=True, encoding='utf-8', "
         b"errors='replace')\n",
         False, "a seam call that already names both is clean"),
        (b"import subprocess\n"
         b"def install(*, runner=subprocess.run):\n"
         b"    runner(x, capture_output=True)\n",
         False, "byte-mode through a seam is still out of scope"),
        (b"import subprocess as sp\n"
         b"def install(*, runner=sp.run):\n"
         b"    runner(x, text=True)\n",
         True, "an aliased subprocess default must still be resolved"),
        (b"def other(*, runner=some_factory()):\n"
         b"    runner(x, text=True)\n",
         False,
         "a parameter whose default is not a bound subprocess method is "
         "not a seam at all"),
        (b"import subprocess\n"
         b"def install(*, runner=subprocess.run):\n"
         b"    other_name(x, text=True)\n",
         False, "a call to an unrelated name is not a seam call"),
    ],
)
def test_the_scanner_flags_injected_runner_seams(source, flagged, why):
    tree = ast.parse(source)
    offenders = _offending_lines(tree)
    assert bool(offenders) is flagged, why


def test_the_guard_sees_the_walks_provision_runner_seam():
    """`install_walks`'s `runner=subprocess.run` parameter is an
    injected-runner seam, not a direct `subprocess.run(...)` call: the call
    site is `runner(step, ...)`, a bare-name call the plain attribute-walk
    in `_is_undeclared_decode_call` cannot see on its own. This is the
    positive control for the SEAM half of the scanner (mirrors
    `test_the_guard_can_see_the_site_that_already_complies` for the direct
    half): proves the scanner actually inspects this specific site and
    finds it clean post-fix, so the flat zero in
    `test_no_subprocess_in_src_decodes_with_the_host_codepage` means the
    seam was checked, not silently skipped the way it was before this
    scanner learned to resolve injected-runner defaults."""
    path = SRC / "walks_provision.py"
    tree = ast.parse(path.read_bytes(), filename=str(path))
    aliases = _subprocess_aliases(tree)
    fn = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == "install_walks"
    )
    assert _seam_param_names(fn, aliases) == {"runner"}, (
        "install_walks's runner=subprocess.run default is no longer "
        "recognised as a seam -- update this pin"
    )
    assert _seam_offenders(tree, aliases) == [], (
        "install_walks's runner(...) call must name both encoding= and "
        "errors=; see the module-wide guard's failure message for the line"
    )


def test_byte_mode_sites_are_left_alone():
    """Two named byte-mode call sites the sweep deliberately did not touch.
    Both decode their own bytes downstream by hand
    (`review/reviewer.py::_file_text` returns `proc.stdout.decode("utf-8",
    errors="replace")`; `testing/runner.py`'s Windows `taskkill` path only
    ever inspects `.returncode`) — adding `encoding=` at the call site would
    silently change their return type from `bytes` to `str`, a behaviour
    change out of scope for this fix. Pinned by exact line so a future edit
    that adds `text=`/`encoding=` here trips this test rather than silently
    widening the guard's blast radius."""
    samples = [
        (SRC / "review" / "reviewer.py", 500),
        (SRC / "testing" / "runner.py", 58),
    ]
    for path, lineno in samples:
        tree = ast.parse(path.read_bytes(), filename=str(path))
        aliases = _subprocess_aliases(tree)
        sites = [n for n in _subprocess_call_sites(tree, aliases) if n.lineno == lineno]
        rel = path.relative_to(REPO_ROOT).as_posix()
        assert len(sites) == 1, (
            f"{rel}:{lineno} is no longer a subprocess call site; "
            "update this pin (and double check it is still byte-mode)"
        )
        kw = {k.arg for k in sites[0].keywords}
        assert not (kw & {"text", "universal_newlines", "encoding"}), (
            f"{rel}:{lineno} gained a text-mode kwarg; this test only "
            "guards it as byte-mode, decide deliberately whether it should "
            "join the guarded set"
        )
        assert _is_undeclared_decode_call(sites[0], aliases) is False


# ---------------------------------------------------------------------------
# AC2: the _readerthread/_communicate mechanism, reproduced without Windows
# ---------------------------------------------------------------------------

def _readerthread(fh, buffer: list) -> None:
    """Line-for-line what CPython's `Popen._readerthread` does
    (`Lib/subprocess.py`): read the whole stream and append it, then close.
    Nothing here catches an exception `fh.read()` raises — exactly as
    upstream — so an undecodable byte under a strict/narrow codec kills this
    thread silently and `buffer` never receives its element."""
    buffer.append(fh.read())
    fh.close()


def _reader_thread_result(raw: bytes, *, encoding: str, errors: str = "strict"):
    """Reproduce the EXACT tail of CPython's `Popen._communicate` for one
    pipe, on any host: decode *raw* through a `TextIOWrapper(encoding,
    errors)` on a background THREAD (the way Windows does it — POSIX
    decodes inline in the foreground and would just raise, which is exactly
    why this needs a real thread to be a faithful reduction rather than a
    POSIX-shaped one), then apply `_communicate`'s own tail:
    `stdout = stdout[0] if stdout else None`.

    This is not a simulation of the bug in the abstract — it is the same
    `_readerthread` function body, the same `TextIOWrapper`, and the same
    empty-list-is-falsy tail CPython's own `subprocess.py` uses, wired to a
    real OS pipe instead of a real child process. The only thing swapped out
    is WHICH operating system supplies the reader thread; the mechanism
    (thread dies on decode error -> buffer list stays `[]` -> tail yields
    `None`) is identical on both.
    """
    read_fd, write_fd = os.pipe()
    try:
        os.write(write_fd, raw)
    finally:
        os.close(write_fd)
    fh = io.TextIOWrapper(io.FileIO(read_fd, "rb"), encoding=encoding, errors=errors)
    buffer: list[str] = []
    thread = threading.Thread(target=_readerthread, args=(fh, buffer))
    thread.start()
    thread.join()
    return buffer[0] if buffer else None


@pytest.mark.filterwarnings("ignore::pytest.PytestUnhandledThreadExceptionWarning")
def test_the_readerthread_reduction_yields_none_under_the_host_codepage():
    result = _reader_thread_result(
        HEBREW_SUBJECT.encode("utf-8"), encoding="cp1255", errors="strict",
    )
    assert result is None, (
        "encoding=<host codepage> with errors='strict' (subprocess's own "
        "default when only text=True is given) must reproduce the exact "
        "failure this fix closes: the reader thread's UnicodeDecodeError "
        "kills it silently, the buffer list stays empty, and "
        "_communicate's tail returns None where the caller expects a string"
    )


def test_the_same_reduction_under_utf8_yields_the_string():
    result = _reader_thread_result(
        HEBREW_SUBJECT.encode("utf-8"), encoding="utf-8", errors="replace",
    )
    assert result == HEBREW_SUBJECT, (
        "encoding='utf-8' (with errors='replace' along for the ride) must "
        "decode valid UTF-8 bytes byte-exact through the same thread/tail "
        "machinery that returned None above"
    )


def test_a_real_subprocess_under_a_narrow_codepage_raises_and_under_utf8_does_not():
    """Closes the gap the reduction above leaves open: it isolates the
    reader-thread TAIL, but does not by itself prove a REAL
    `subprocess.run(..., text=True, encoding=...)` behaves the way the
    reduction assumes. This uses a real child process and real pipes.

    On POSIX (this test's host) there is no reader thread — `_communicate`
    decodes inline in the FOREGROUND, so the same bad byte raises
    `UnicodeDecodeError` directly to the caller rather than yielding `None`.
    That is the faithful, not identical, half of the reduction: Windows
    relays the identical `UnicodeDecodeError` through the identical
    `TextIOWrapper` codec machinery, but from inside a thread whose
    exception `_communicate` never re-raises, so the SAME root cause
    surfaces as `None` there and as a raise here. Either way, the call must
    never silently degrade to less text than was written — either it raises
    loudly (POSIX today) or, after this fix, it returns the full string.
    """
    child_source = (
        "import sys; sys.stdout.buffer.write("
        + repr(HEBREW_SUBJECT.encode("utf-8"))
        + ")"
    )

    proc = subprocess.run(
        [sys.executable, "-c", child_source],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    assert proc.stdout == HEBREW_SUBJECT, (
        "encoding='utf-8', errors='replace' must return the child's output "
        "byte-exact when the bytes are valid UTF-8"
    )

    with pytest.raises(UnicodeDecodeError):
        subprocess.run(
            [sys.executable, "-c", child_source],
            capture_output=True, text=True, encoding="cp1255", errors="strict",
        )


def test_the_subject_is_undecodable_as_cp1255():
    raw = HEBREW_SUBJECT.encode("utf-8")
    raw.decode("utf-8")  # sanity: valid UTF-8, nothing wrong with the text itself
    with pytest.raises(UnicodeDecodeError):
        raw.decode("cp1255")


# ---------------------------------------------------------------------------
# AC3: the git call path specifically — round-trips through real GitRepo code
# ---------------------------------------------------------------------------

def _git_repo(path):
    """A minimal real repo with one commit, on a non-protected branch —
    mirrors `tests/test_git.py::_git_repo` (not imported from there: that
    module has no public re-export contract, and duplicating six lines here
    keeps this file self-contained)."""
    from no_human.vcs import GitRepo

    subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True)
    (path / "a.txt").write_text("1\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(path), "add", "-A"], check=True)
    subprocess.run(
        ["git", "-C", str(path), "-c", "user.name=no_human",
         "-c", "user.email=no-human@acme.com", "commit", "-m", "init"],
        check=True,
    )
    repo = GitRepo(path)
    repo.create_branch("wip/git-test")
    return repo


def test_a_hebrew_commit_subject_round_trips_through_git_repo(tmp_path):
    """`GitRepo.commit_all` writes the subject, `GitRepo.commit_subjects`
    reads it back through `_run` (`git log --format=%s`) — the exact call
    path `_sh`'s docstring in `approve_merge.py` describes. Asserts the
    RETURNED TEXT equals the original, not merely that nothing raised."""
    repo = _git_repo(tmp_path)
    (tmp_path / "a.txt").write_text("2\n", encoding="utf-8")
    repo.commit_all(f"feat: {HEBREW_SUBJECT}")

    subjects = repo.commit_subjects("main")
    assert subjects == [f"feat: {HEBREW_SUBJECT}"], (
        "commit_subjects must return the Hebrew subject byte-exact, not "
        "None (which its own try/except would otherwise mask as an empty "
        "list) and not a mojibake corruption of it"
    )


def test_a_hebrew_path_round_trips_through_run_null(tmp_path):
    """`uncommitted_source_files` feeds `_run_null`'s `-z`-terminated output
    straight back into a comparison against filesystem paths
    (`_null_paths`). A Hebrew-named uncommitted source file must come back
    byte-exact — `_run_null` never `.strip()`s (see its own docstring: that
    would eat the first path's leading byte), so this also indirectly pins
    that AC4 contract stayed intact."""
    repo = _git_repo(tmp_path)
    hebrew_name = "תיקון_קידוד.py"
    (tmp_path / hebrew_name).write_text("# x\n", encoding="utf-8")

    leftovers = repo.uncommitted_source_files()
    assert leftovers == [hebrew_name], (
        "an untracked Hebrew-named source file must come back byte-exact "
        "through _run_null/_null_paths — None here would raise inside "
        "_null_paths' out.split('\\0'), and a wrong codec would silently "
        "corrupt the name into something that never matches the real path "
        "on disk"
    )


def test_a_subject_written_as_undecodable_bytes_never_returns_none(tmp_path):
    """Bytes that are not valid UTF-8 AT ALL (not merely non-cp1255) must
    still come back as a *string* — degraded via U+FFFD, never None and
    never a raised UnicodeDecodeError escaping GitRepo.

    Built via `git hash-object -w --stdin -t commit` on a hand-assembled
    commit object, not `git commit-tree -F -`: `commit-tree` runs git's own
    UTF-8 validation (`strbuf_utf8_fixup`), which silently RE-ENCODES an
    invalid message through a fallback codec before it is ever stored —
    confirmed empirically (bytes `FF FE` in, `C3 BF C3 BE` — valid UTF-8 —
    out), so it can never plant genuinely undecodable bytes in the first
    place. The plumbing command stores whatever bytes it is given,
    unvalidated, which is what this test needs to exist at all.
    """
    repo = _git_repo(tmp_path)
    tree = subprocess.run(
        ["git", "-C", str(tmp_path), "write-tree"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    parent = repo.head_sha()
    when = "1700000000 +0000"
    ident = f"no_human <no-human@acme.com> {when}"
    commit_object = (
        f"tree {tree}\n"
        f"parent {parent}\n"
        f"author {ident}\n"
        f"committer {ident}\n"
        f"\n"
    ).encode("ascii") + b"broken: \xff\xfe not valid utf-8 at all\n"
    hash_proc = subprocess.run(
        ["git", "-C", str(tmp_path), "hash-object", "-w", "--stdin", "-t", "commit"],
        input=commit_object, capture_output=True, check=True,
    )
    new_sha = hash_proc.stdout.decode().strip()
    subprocess.run(
        ["git", "-C", str(tmp_path), "update-ref", "HEAD", new_sha], check=True,
    )

    subjects = repo.commit_subjects("main")
    assert subjects, (
        "commit_subjects must surface a subject for this commit rather than "
        "silently emptying out"
    )
    assert isinstance(subjects[-1], str)
    assert "�" in subjects[-1], (
        "invalid UTF-8 bytes must degrade to U+FFFD replacement characters "
        "-- never raise UnicodeDecodeError and never come back as None"
    )


# ---------------------------------------------------------------------------
# AC4: no defensive None-guard was introduced at the strip()/consumer sites
# ---------------------------------------------------------------------------

def test_no_none_guard_was_added_at_the_strip_sites():
    """The fix closes the None-return class at its CAUSE (encoding= +
    errors= on the subprocess call itself), never by adding `or ""` / `is
    None` defensive guards where the result is consumed — a guard there
    would hide a regression in the fix rather than prevent one, and would
    contradict the acceptance criteria this test enforces directly."""
    src = (SRC / "vcs" / "git.py").read_text(encoding="utf-8")
    tree = ast.parse(src, filename="git.py")

    def _tail(fn_name: str) -> ast.expr:
        fn = next(
            n for n in ast.walk(tree)
            if isinstance(n, ast.FunctionDef) and n.name == fn_name
        )
        last = fn.body[-1]
        assert isinstance(last, ast.Return), f"{fn_name}'s last statement is not a bare return"
        assert last.value is not None
        return last.value

    run_tail = _tail("_run")
    assert isinstance(run_tail, ast.Call), "_run must still end in a bare call, not a guarded expression"
    assert isinstance(run_tail.func, ast.Attribute) and run_tail.func.attr == "strip"
    receiver = run_tail.func.value
    assert isinstance(receiver, ast.Attribute) and receiver.attr == "stdout", (
        "_run's tail must stay proc.stdout.strip() -- not "
        "(proc.stdout or \"\").strip() or any other None-guard"
    )
    assert isinstance(receiver.value, ast.Name) and receiver.value.id == "proc"

    run_null_tail = _tail("_run_null")
    assert isinstance(run_null_tail, ast.Attribute) and run_null_tail.attr == "stdout", (
        "_run_null's tail must stay bare proc.stdout -- no .strip(), no "
        "None-guard (see its own docstring on why .strip() is forbidden here)"
    )
    assert isinstance(run_null_tail.value, ast.Name) and run_null_tail.value.id == "proc"


# ---------------------------------------------------------------------------
# AC5: errors= alone is insufficient without encoding= (review_routing.py)
# ---------------------------------------------------------------------------

def test_errors_alone_does_not_satisfy_the_guard():
    """`errors=` without `encoding=` does not raise — it decodes with
    whatever codec `encoding=` would otherwise have pinned (here, standing
    in for the host locale codepage), silently producing MOJIBAKE instead of
    a crash. `errors="replace"` only ever fires on a byte the chosen codec
    cannot decode; a codec that decodes every byte (as most single-byte
    code pages do) never invokes it at all, so `errors=` alone is not even
    inert here — it does nothing while the wrong codec quietly corrupts the
    text. This is exactly the failure `core/review_routing.py`'s three
    original call sites had (`errors="replace"` present, `encoding=`
    absent) before this fix, and why the AST guard above requires the pair.
    """
    utf8_bytes = HEBREW_SUBJECT.encode("utf-8")

    # latin-1 maps every byte 0x00-0xFF, so it never raises -- it just
    # mis-decodes UTF-8's continuation bytes into unrelated latin-1
    # characters. This is the concrete SHAPE of "errors= alone corrupts
    # rather than crashes": nothing here ever reaches the errors= handler.
    mojibake = utf8_bytes.decode("latin-1")
    assert mojibake != HEBREW_SUBJECT

    assert utf8_bytes.decode("latin-1", errors="replace") == mojibake, (
        "errors='replace' changes nothing about this failure: latin-1 never "
        "raises in the first place, so 'replace' never fires -- the "
        "corruption already happened at the codec choice, not at an "
        "undecodable byte, which is exactly why errors= alone cannot fix it"
    )


def test_review_routing_changed_entries_returns_a_utf8_path(tmp_path):
    """`review_routing.changed_entries` is one of the three sites that had
    `errors="replace"` with no `encoding=` before this fix. `core.quotePath`
    is set to `false` on THIS test repo only to expose the raw non-ASCII
    bytes `git diff --name-status` writes (git quotes non-ASCII path bytes
    by default, octal-escaped — an unrelated feature this test must not
    conflate with the DECODING step actually under test); the fix itself
    changes nothing about quoting."""
    from no_human.core.review_routing import changed_entries

    repo = _git_repo(tmp_path)
    subprocess.run(
        ["git", "-C", str(tmp_path), "config", "core.quotePath", "false"], check=True,
    )
    before = repo.head_sha()
    hebrew_name = "קובץ_חדש.py"
    (tmp_path / hebrew_name).write_text("# x\n", encoding="utf-8")
    repo.commit_all(f"feat: add {hebrew_name}")
    after = repo.head_sha()

    entries = changed_entries(tmp_path, before, after)
    paths = [e.path for e in entries]
    assert hebrew_name in paths, (
        "changed_entries must decode git diff --name-status output as "
        "UTF-8 byte-exact; before this fix, errors='replace' alone decoded "
        "it with the host locale codepage instead, which -- for a codec "
        "that cannot represent these bytes -- would corrupt them into "
        "mojibake rather than raise"
    )


# ---------------------------------------------------------------------------
# AC6: approve_merge.py is explicitly untouched, and already compliant
# ---------------------------------------------------------------------------

def test_approve_merge_sh_already_names_its_encoding():
    """`src/no_human/vcs/approve_merge.py` is deliberately NOT touched by
    this fix's sweep: its one subprocess call site, `_sh` (line 233),
    already carries both `encoding="utf-8"` and `errors="replace"` — landed
    ahead of this change, per its own docstring's explanation of the
    identical `_readerthread` mechanism this module's AC2 tests reproduce.
    Coordinating a second edit there risks colliding with the interpreter
    fix in flight in this same file (`_run_pytest`'s
    `PYTHONIOENCODING=utf-8` env-copy, guarding the Python-child case `_sh`
    itself does not cover), so it is left alone rather than re-touched."""
    path = SRC / "vcs" / "approve_merge.py"
    tree = ast.parse(path.read_bytes(), filename=str(path))
    aliases = _subprocess_aliases(tree)
    sites = _subprocess_call_sites(tree, aliases)

    assert [n.lineno for n in sites] == [233], (
        "approve_merge.py's subprocess surface changed since this fix was "
        "written -- re-verify whether it still needs no edit"
    )
    kw = {k.arg for k in sites[0].keywords}
    assert kw & {"encoding"} and kw & {"errors"}, (
        "_sh no longer names both encoding= and errors= -- approve_merge.py "
        "would need to join this fix's sweep after all"
    )
    literal_values = {
        k.arg: (k.value.value if isinstance(k.value, ast.Constant) else None)
        for k in sites[0].keywords
    }
    assert literal_values.get("encoding") == "utf-8"
    assert literal_values.get("errors") == "replace"
