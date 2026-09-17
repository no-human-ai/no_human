r"""`GitRepo`'s subprocess reads decode as UTF-8, never the host codepage.

Measured on Windows (he-IL, cp1255) against the shipped 0.2.3 desktop build:
a task titled with Hebrew reached the commit and then took the whole run down.

    File "subprocess.py", line 1599, in _readerthread
    File "encodings\cp1255.py", line 23, in decode
    UnicodeDecodeError: 'charmap' codec can't decode byte 0x9c in position 91
    ...
    File "no_human/vcs/git.py", line 262, in _run
    AttributeError: 'NoneType' object has no attribute 'strip'

`git commit` echoes the subject back, the Hebrew letter lamed is UTF-8 `D7 9C`,
and `0x9C` is undefined in cp1255. With `text=True` alone the decode uses the
LOCALE codec, so it raises. On Windows `Popen._readerthread` reads each pipe in
a THREAD, so the exception kills the THREAD rather than reaching the caller:
`_communicate` joins an already-dead thread without raising, the buffer stays
empty, and its tail — `stdout[0] if stdout else None` — yields None. `_run`
then calls `.strip()` on None. The user's work is left on its branch with no PR
and the task never reaches approval.

Same fix, and the same reasoning, as `vcs/approve_merge.py::_sh` (468f1f12):
`encoding` AND `errors`, not one or the other. git emits UTF-8 regardless of
the host codepage, so `encoding="utf-8"` is the correction; `errors="replace"`
is the net that keeps a malformed byte from ever raising here again.

Both tests below fail against the unfixed module, which is the only thing that
makes them worth having. The round-trip one forces cp1255 explicitly rather
than relying on the host's own locale, so it is load-bearing on a UTF-8 CI
runner too — where this bug is invisible.
"""

import ast
import locale
import subprocess
from pathlib import Path

import pytest

from no_human.vcs import GitRepo
from no_human.vcs import git as git_mod

# Hebrew + a Latin-1 accent. Lamed carries the 0x9C that cp1255 cannot decode;
# the e-acute is the byte that a cp1251 host would die on instead.
HEBREW_SUBJECT = "חלוקה café"


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def repo_with_non_ascii_subject(tmp_path):
    """A repo whose HEAD commit subject is non-ASCII, written as UTF-8 bytes.

    The message goes through a FILE, not `-m`: the point under test is how the
    PARENT decodes what git prints, and routing the subject through argv would
    drag the platform's argv encoding into a test that is not about that.
    """
    work = tmp_path / "work"
    work.mkdir()
    _git(work, "init", "-b", "main")
    _git(work, "config", "user.email", "u@example.com")
    _git(work, "config", "user.name", "u")
    (work / "app.py").write_text("x = 1\n")
    _git(work, "add", "-A")
    msg = tmp_path / "msg.txt"
    msg.write_bytes(HEBREW_SUBJECT.encode("utf-8"))
    _git(work, "commit", "-F", str(msg))
    return work


def test_non_ascii_subject_round_trips_under_a_non_utf8_locale(
    repo_with_non_ascii_subject, monkeypatch
):
    """The exact 0.2.3 crash, reproduced on any host by forcing the codepage.

    Asserts the SUBJECT COMES BACK INTACT, not merely that nothing raised:
    `errors="replace"` alone would satisfy a no-crash assertion while handing
    the caller mojibake, and a commit subject is read by humans.
    """
    monkeypatch.setattr(locale, "getpreferredencoding", lambda *a, **k: "cp1255")
    if hasattr(locale, "getencoding"):
        monkeypatch.setattr(locale, "getencoding", lambda *a, **k: "cp1255")

    repo = GitRepo(repo_with_non_ascii_subject)
    subject = repo._run("log", "-1", "--pretty=%s")

    assert subject == HEBREW_SUBJECT


def _text_mode_subprocess_calls(path: Path):
    """(lineno, keyword-names) for every `subprocess.*` call in *path*."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Attribute):
            continue
        if getattr(func.value, "id", None) != "subprocess":
            continue
        if func.attr not in ("run", "Popen", "check_output", "call"):
            continue
        yield node.lineno, {kw.arg for kw in node.keywords if kw.arg}


def test_every_text_mode_git_subprocess_declares_utf8():
    """The guard that stops the 16th site from drifting back.

    A per-call-site assertion rather than a behavioural one because the sites
    are the thing that rots: `_run` is not the only reader in this module, and
    a new `subprocess.run(..., text=True)` added later would reintroduce the
    crash in a code path no round-trip test happens to cover. Binary reads are
    asserted to stay binary — an `encoding` on a blob read corrupts it.
    """
    path = Path(git_mod.__file__)
    offenders, wrongly_decoded = [], []
    for lineno, kwargs in _text_mode_subprocess_calls(path):
        text_mode = "text" in kwargs or "universal_newlines" in kwargs
        if text_mode and not ("encoding" in kwargs and "errors" in kwargs):
            offenders.append(lineno)
        if not text_mode and "encoding" in kwargs:
            wrongly_decoded.append(lineno)

    assert not offenders, (
        f"{path.name}: text-mode subprocess calls without an explicit "
        f"encoding/errors at lines {offenders} — these decode with the host "
        f"codepage and raise on non-ASCII git output"
    )
    assert not wrongly_decoded, (
        f"{path.name}: byte-mode subprocess calls carrying an encoding at "
        f"lines {wrongly_decoded} — decoding a binary git read corrupts it"
    )
