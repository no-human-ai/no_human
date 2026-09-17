r"""Subprocess output decodes with the host codepage, losing non-ASCII.

THE CHAIN (measured on Windows, he-IL, cp1255, against the shipped 0.2.3
desktop build — the same incident `test_git_decodes_utf8.py` documents for
`vcs/git.py` specifically):

    File "subprocess.py", line 1599, in _readerthread
        buffer.append(fh.read())
    File "encodings\cp1255.py", line 23, in decode
        UnicodeDecodeError: 'charmap' codec can't decode byte 0x9c ...

On Windows, `Popen._communicate` reads each pipe in a background THREAD via
`_readerthread` (`buffer.append(fh.read()); fh.close()`). With `text=True` (or
any of `universal_newlines=True` / a bare `encoding=` / a bare `errors=`) and
no *explicit* `encoding=`, that `fh.read()` decodes using the HOST's ANSI
codepage. A UTF-8 byte with no mapping in that codepage — `0x9C`, part of the
UTF-8 encoding of a Hebrew letter — raises `UnicodeDecodeError` INSIDE the
thread. `_communicate` then joins the now-dead thread WITHOUT re-raising: the
buffer stays empty, and its tail — `stdout[0] if stdout else None` — quietly
becomes `None`. A caller that does `stdout.strip()` then raises
`AttributeError: 'NoneType' object has no attribute 'strip'` on the PR-open
path (fatal, no PR), or a caller that guards `if stdout:` silently treats a
crashed read as "nothing changed" on the approve/merge path (survivable, but
wrong — a PR merged despite two dead reader threads).

The fix, uniform everywhere: an explicit `encoding="utf-8"` (git always emits
UTF-8, regardless of host codepage) AND an explicit `errors="replace"` (the
net that keeps a still-malformed byte from ever raising here again) — matching
the precedent already shipped in `vcs/git.py::_run` and
`vcs/approve_merge.py::_sh` (`468f1f12`). `errors` alone is NOT enough: it
governs what the *chosen* codec rejects, but the codec is still the host
codepage, so `errors="replace"` without `encoding="utf-8"` silently mangles
non-ASCII bytes into mojibake instead of crashing on them (AC5, exercised
concretely against `core/review_routing.py` below).

This module is BOTH the enumerator that drove the sweep AND the regression
guard that keeps it from rotting: `find_sites`/`classify` below are the exact
logic used to produce the fix's offender list (alias-aware, deferred-call-
aware via `asyncio.to_thread(subprocess.run, ...)`, and injected-seam-aware
via `runner=subprocess.run`-style dependency injection). A scanner that
silently sees nothing would go green forever without proving anything, so
`test_the_scanner_sees_a_site_that_already_complies` pins it against known
offending shapes, known compliant shapes, AND two known false-positive
lookalikes (`AgentEvent(text=...)`, `chat_postMessage(text=...)` — unrelated
APIs that merely happen to have a `text` keyword).
"""

from __future__ import annotations

import ast
import io
import locale
import subprocess
import sys
import threading
from pathlib import Path

import pytest

import no_human
from no_human.core import review_routing
from no_human.vcs.git import GitRepo

SRC_ROOT = Path(no_human.__file__).parent

# ---------------------------------------------------------------------------
# The enumerator (also the guard). Transcribed from the dev-time scan used to
# produce the fix's offender list — see the PR body for the full method.
# ---------------------------------------------------------------------------

SUBPROCESS_METHODS = {"run", "Popen", "check_output", "check_call", "call"}
TEXT_SIGNAL_KWARGS = {"text", "universal_newlines", "encoding", "errors"}
DEFERRED_CALLEES = {"to_thread"}  # asyncio.to_thread(subprocess.run, ...)


def _subprocess_aliases(tree: ast.AST) -> set[str]:
    aliases = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "subprocess":
                    aliases.add(alias.asname or "subprocess")
    return aliases


def _is_subprocess_attr(node: ast.AST, aliases: set[str]) -> bool:
    return (
        isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id in aliases
        and node.attr in SUBPROCESS_METHODS
    )


def _seam_param_names(tree: ast.AST, aliases: set[str]):
    """function node -> {param names whose DEFAULT is subprocess.<method>}."""
    seams = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            args = node.args
            all_pos = list(args.posonlyargs) + list(args.args)
            defaults = args.defaults
            offset = len(all_pos) - len(defaults)
            names = set()
            for i, d in enumerate(defaults):
                if _is_subprocess_attr(d, aliases):
                    names.add(all_pos[offset + i].arg)
            for a, d in zip(args.kwonlyargs, args.kw_defaults):
                if d is not None and _is_subprocess_attr(d, aliases):
                    names.add(a.arg)
            if names:
                seams[node] = names
    return seams


class Site:
    __slots__ = ("lineno", "kwargs", "kind", "call")

    def __init__(self, lineno, kwargs, kind, call):
        self.lineno = lineno
        self.kwargs = kwargs
        self.kind = kind  # "direct" | "deferred" | "seam"
        self.call = call


def _sites_in_tree(tree: ast.AST) -> list[Site]:
    aliases = _subprocess_aliases(tree)
    seams = _seam_param_names(tree, aliases)

    sites: list[Site] = []
    seen_call_ids: set[int] = set()

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        kwargs = {kw.arg for kw in node.keywords if kw.arg}

        if _is_subprocess_attr(func, aliases):
            sites.append(Site(node.lineno, kwargs, "direct", node))
            seen_call_ids.add(id(node))
            continue

        if (
            isinstance(func, ast.Attribute)
            and func.attr in DEFERRED_CALLEES
            and node.args
            and _is_subprocess_attr(node.args[0], aliases)
        ):
            sites.append(Site(node.lineno, kwargs, "deferred", node))
            seen_call_ids.add(id(node))
            continue

    for fn, names in seams.items():
        for node in ast.walk(fn):
            if node is fn or not isinstance(node, ast.Call):
                continue
            if id(node) in seen_call_ids:
                continue
            if isinstance(node.func, ast.Name) and node.func.id in names:
                kwargs = {kw.arg for kw in node.keywords if kw.arg}
                sites.append(Site(node.lineno, kwargs, "seam", node))
                seen_call_ids.add(id(node))

    return sites


def find_sites(path: Path) -> list[Site]:
    src = path.read_text(encoding="utf-8")
    tree = ast.parse(src, filename=str(path))
    return _sites_in_tree(tree)


def classify(site: Site) -> str:
    signal = site.kwargs & TEXT_SIGNAL_KWARGS
    if not signal:
        return "byte"
    if {"encoding", "errors"} <= site.kwargs:
        return "compliant"
    return "offender"


def _all_sites():
    """(path, Site) for every subprocess call site under src/no_human."""
    files = sorted(SRC_ROOT.rglob("*.py"))
    out = []
    for f in files:
        for s in find_sites(f):
            out.append((f, s))
    return files, out


# ---------------------------------------------------------------------------
# AC1 — every text-mode call site declares utf-8 + an errors policy.
# ---------------------------------------------------------------------------


def test_every_text_mode_subprocess_call_declares_utf8():
    """The core regression guard: no text-mode subprocess call under
    `src/no_human` may omit an explicit `encoding`/`errors` pair. This is the
    same predicate that drove the sweep — running it against the pre-fix tree
    lists 118 offenders across 51 files; against the fixed tree it is empty."""
    _, sites = _all_sites()
    offenders = [
        f"{path.relative_to(SRC_ROOT.parent.parent)}:{site.lineno}"
        for path, site in sites
        if classify(site) == "offender"
    ]
    assert offenders == [], (
        "text-mode subprocess call(s) without both encoding= and errors=:\n"
        + "\n".join(offenders)
    )


def test_no_binary_call_carries_an_encoding():
    """The other direction, asserted explicitly: a byte-mode call (no
    text=/universal_newlines=/encoding=/errors= signal) must carry NO
    encoding. Adding one would flip the return type bytes -> str and break
    every `.decode(...)` caller (e.g. `eval/northstar.py`'s checkout read,
    `api/app.py`'s pkill reaper, `core/orchestrator.py`'s fetch)."""
    _, sites = _all_sites()
    wrongly_decoded = [
        f"{path.relative_to(SRC_ROOT.parent.parent)}:{site.lineno}"
        for path, site in sites
        if classify(site) == "byte" and "encoding" in site.kwargs
    ]
    assert wrongly_decoded == [], (
        "byte-mode subprocess call(s) carrying an encoding:\n"
        + "\n".join(wrongly_decoded)
    )


def test_the_scanner_covers_every_file_under_src():
    """A scanner that silently skips a subtree would go green by seeing
    nothing. Pin that every `.py` file under `src/no_human` was actually
    walked, not just that the offender list came back empty."""
    files, _ = _all_sites()
    assert files == sorted(SRC_ROOT.rglob("*.py"))
    assert len(files) > 200, "suspiciously few files walked — src/no_human root moved?"


def test_the_scanner_sees_a_site_that_already_complies():
    """Positive control (AC1/AC6) + false-positive control, both required
    because a scanner that always returns [] "passes" AC1 for the wrong
    reason. Five offending shapes must be flagged; the compliant shape and
    two unrelated-API lookalikes must not; and two REAL files must be reached
    and classified compliant — proving the scanner can see code that already
    does the right thing, not just code that's missing something."""
    synthetic = '''
import asyncio
import subprocess
import subprocess as _sp


def f1():
    subprocess.run(["x"], text=True)  # line 8: bare text=True -> offender


def f2():
    subprocess.run(["x"], errors="replace")  # line 12: errors-only -> offender


def f3():
    _sp.run(["x"], text=True)  # line 16: aliased -> offender


async def f4():
    await asyncio.to_thread(subprocess.run, ["x"], text=True)  # line 20: deferred -> offender


def f5(runner=subprocess.run):
    runner(["x"], text=True)  # line 24: seam -> offender


def f6():
    subprocess.run(["x"], text=True, encoding="utf-8", errors="replace")  # line 28: compliant


def f7():
    AgentEvent(text="hi")  # line 32: false positive -- unrelated API


def f8():
    chat_postMessage(text="hi")  # line 36: false positive -- unrelated API
'''
    tree = ast.parse(synthetic, filename="<synthetic>")
    sites = {s.lineno: s for s in _sites_in_tree(tree)}

    offender_lines = {8, 12, 16, 20, 24}
    for lineno in offender_lines:
        assert lineno in sites, f"line {lineno} was never recognised as a subprocess call"
        assert classify(sites[lineno]) == "offender", (
            f"line {lineno}: expected offender, got {classify(sites[lineno])}"
        )

    assert 28 in sites and classify(sites[28]) == "compliant"

    # The lookalikes must never even become Sites -- their `func` is not a
    # subprocess attribute, deferred call, or seam parameter.
    assert 32 not in sites, "AgentEvent(text=...) was mistaken for a subprocess call"
    assert 36 not in sites, "chat_postMessage(text=...) was mistaken for a subprocess call"

    # Real-file half: the scanner must actually reach the two files that are
    # the OUT-OF-SCOPE positive controls and classify every site compliant.
    git_sites = find_sites(SRC_ROOT / "vcs" / "git.py")
    assert len(git_sites) >= 16, f"expected >=16 sites in vcs/git.py, found {len(git_sites)}"
    assert all(classify(s) == "compliant" for s in git_sites), (
        "vcs/git.py has a non-compliant subprocess site -- it must stay untouched and compliant"
    )
    assert any(s.lineno == 283 for s in git_sites)

    merge_sites = find_sites(SRC_ROOT / "vcs" / "approve_merge.py")
    assert any(s.lineno == 233 and classify(s) == "compliant" for s in merge_sites), (
        "approve_merge.py:233 (_sh) is expected untouched and already compliant"
    )


# ---------------------------------------------------------------------------
# AC2 — the Windows reader-thread mechanism, reproduced without a Windows
# host.
# ---------------------------------------------------------------------------


def _readerthread(fh, buffer):
    """CPython's `Popen._readerthread`, verbatim (subprocess.py, Windows-only
    branch): read the whole pipe, append to the shared list, close. If
    `fh.read()` raises, the exception propagates inside the THREAD and is
    never seen by the caller -- `buffer` simply never gets its entry."""
    buffer.append(fh.read())
    fh.close()


def _reduce_through_a_reader_thread(payload: bytes, encoding: str, errors: str | None = None):
    """Runs `_readerthread` in a real background thread over a `TextIOWrapper`
    decoding *payload* as *encoding*, then applies `_communicate`'s own tail
    reduction (`stdout[0] if stdout else None`) -- the exact two lines that
    turn a dead thread into `None` instead of a raised exception."""
    kwargs = {"errors": errors} if errors is not None else {}
    fh = io.TextIOWrapper(io.BytesIO(payload), encoding=encoding, **kwargs)
    buffer: list[str] = []
    t = threading.Thread(target=_readerthread, args=(fh, buffer))
    t.daemon = True
    t.start()
    t.join()
    return buffer[0] if buffer else None


@pytest.mark.filterwarnings(
    "ignore::pytest.PytestUnhandledThreadExceptionWarning"
)
def test_reader_thread_reduction_yields_none_under_a_host_codepage():
    """This is the mechanism, not a simulation of it: `_readerthread` is
    CPython's actual code (subprocess.py), run in an actual `threading.Thread`
    over an actual `io.TextIOWrapper` -- the same three primitives
    `Popen._communicate` uses on Windows. Swapping the *codec* used to
    construct the pipe for cp1255 is the ONLY thing this test does
    differently from a real Windows host, because a real host's codepage is
    exactly the `encoding` argument `TextIOWrapper`/`_readerthread` are
    handed -- nothing about `_readerthread`'s behaviour depends on the OS."""
    payload = "חלוקה".encode("utf-8")  # contains the 0x9C byte cp1255 rejects

    unfixed = _reduce_through_a_reader_thread(payload, encoding="cp1255")
    assert unfixed is None, (
        "expected the dead-thread reduction (None) under the unfixed shape "
        f"(bare host-codepage decode), got {unfixed!r}"
    )

    fixed = _reduce_through_a_reader_thread(payload, encoding="utf-8", errors="replace")
    assert fixed == "חלוקה"


def test_a_text_mode_call_survives_a_cp1255_preferred_encoding(monkeypatch):
    """The live version of the same mechanism, through the REAL public API:
    monkeypatch the preferred-encoding lookup subprocess falls back to when no
    `encoding=` is given, spawn a real child that writes undecodable-under-
    cp1255 UTF-8 bytes, and confirm the FIXED call shape (explicit
    encoding="utf-8", errors="replace") returns the exact string rather than
    None or mojibake."""
    monkeypatch.setattr(locale, "getpreferredencoding", lambda *a, **k: "cp1255")
    if hasattr(locale, "getencoding"):
        monkeypatch.setattr(locale, "getencoding", lambda *a, **k: "cp1255")

    payload = "חלוקה".encode("utf-8")
    code = f"import sys; sys.stdout.buffer.write({payload!r})"
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    assert result.stdout == "חלוקה"


def test_control_unfixed_shape_still_raises(monkeypatch):
    """Premise-pin: proves the `locale` monkeypatch above actually bites on
    THIS host (which is UTF-8, where the bug would otherwise be invisible).
    Without this control, a host where the monkeypatch is a no-op would make
    the "survives" test above pass for the wrong reason -- it would already
    have been passing pre-fix too."""
    monkeypatch.setattr(locale, "getpreferredencoding", lambda *a, **k: "cp1255")
    if hasattr(locale, "getencoding"):
        monkeypatch.setattr(locale, "getencoding", lambda *a, **k: "cp1255")

    payload = "חלוקה".encode("utf-8")
    code = f"import sys; sys.stdout.buffer.write({payload!r})"
    with pytest.raises(UnicodeDecodeError):
        subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)


# ---------------------------------------------------------------------------
# AC3 — the review_routing.py call path specifically.
# ---------------------------------------------------------------------------


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def test_non_ascii_path_round_trips_through_review_routing(tmp_path):
    """`changed_entries` routes review decisions on FILENAMES (guard/scrub,
    security-path detection). A mangled non-ASCII path here doesn't crash --
    it silently mis-routes a security-sensitive rename around the checks that
    exist to catch it (AC5's exact failure mode, exercised end to end).
    Asserts the RETURNED PATH TEXT, not merely that changed_entries didn't
    raise."""
    work = tmp_path / "work"
    work.mkdir()
    _git(work, "init", "-b", "main")
    _git(work, "config", "user.email", "u@example.com")
    _git(work, "config", "user.name", "u")
    # `core.quotePath` C-quotes non-ASCII bytes in `--name-status` output by
    # default -- an orthogonal git behaviour, not this bug. Disabling it
    # isolates the DECODE bug under test (host-codepage vs. explicit utf-8)
    # from that separate quoting convention, the same way the fixture in
    # test_git_decodes_utf8.py routes the commit message through a FILE to
    # isolate parent-decode from argv-encode.
    _git(work, "config", "core.quotePath", "false")

    original_name = "café.py"
    renamed_name = "חלוקה.py"
    (work / original_name).write_text("x = 1\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-m", "add")
    before = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=work, capture_output=True, text=True,
        encoding="utf-8", errors="replace", check=True,
    ).stdout.strip()

    (work / original_name).rename(work / renamed_name)
    _git(work, "add", "-A")
    _git(work, "commit", "-m", "rename")
    after = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=work, capture_output=True, text=True,
        encoding="utf-8", errors="replace", check=True,
    ).stdout.strip()

    entries = review_routing.changed_entries(work, before, after)
    paths = {e.path for e in entries}
    assert renamed_name in paths, (
        f"expected {renamed_name!r} among routed paths, got {sorted(paths)}"
    )


# ---------------------------------------------------------------------------
# AC4 — no defensive None-guard was added anywhere the fix touched.
# ---------------------------------------------------------------------------


def _strip_site_has_or_fallback(func_src: str) -> bool:
    """True if *func_src* (a function's source) returns/uses `proc.stdout`
    guarded by a `BoolOp` (`or ...`) rather than a bare attribute access."""
    tree = ast.parse(func_src)
    for node in ast.walk(tree):
        if isinstance(node, ast.BoolOp) and isinstance(node.op, ast.Or):
            for value in node.values:
                if (
                    isinstance(value, ast.Attribute)
                    and value.attr == "stdout"
                ) or (
                    isinstance(value, ast.Call)
                    and isinstance(value.func, ast.Attribute)
                    and isinstance(value.func.value, ast.Attribute)
                    and value.func.value.attr == "stdout"
                ):
                    return True
    return False


def test_no_strip_site_gained_a_defensive_default():
    """A `None`-guard converts a loud crash (AttributeError on the PR-open
    path) into a silently-empty string a downstream decision reads as a
    legitimate empty result -- exactly the approve/merge path that already
    merged a PR on two dead reader threads. The fix must add ONLY the two
    kwargs, never a guard. Checked on the three git readers directly (their
    docstrings explicitly forbid a defensive strip), by AST rather than text
    match so a reformatted-but-equivalent guard still gets caught."""
    import inspect
    import textwrap

    for name in ("_run", "_run_porcelain_lines", "_run_null"):
        src = textwrap.dedent(inspect.getsource(getattr(GitRepo, name)))
        assert not _strip_site_has_or_fallback(src), (
            f"GitRepo.{name} gained an `or`-guarded proc.stdout fallback"
        )

    assert "proc.stdout.strip()" in inspect.getsource(GitRepo._run)
