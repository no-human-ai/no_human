"""How many ways a command string can be read, on either platform.

Issue #105. Both command guards tokenise with POSIX `shlex`, where `\\` is an
escape character. On Windows that deletes every separator in a native path
before resolution is attempted -- `C:\\Users\\me\\.venv\\Scripts\\pip.exe`
arrives as `C:Usersmeproj.venvScriptspip.exe` -- so the venv guard's
allow-and-log fallback stops being an edge case and becomes the default. The
quieter half is worse than the loud one: after mangling there is no `/` left
in the token, so `_resolve_installer` takes the `shutil.which` branch and not
even the WARNING fires.

WHY THIS RETURNS A LIST RATHER THAN NORMALISING IN PLACE. Which shell runs
the coder's Bash tool on Windows is not settled. Under Git Bash `\\` really is
an escape and POSIX lexing is CORRECT; under cmd or PowerShell it is not.
Picking either reading alone would be a guess, and guessing "backslash is
literal" would open a fresh bypass: `nh\\ approve` is one token under Git Bash
and two under a literal reading, so a guard that only read it literally would
stop recognising a command Git Bash would happily run.

So callers check every reading and deny if ANY of them denies. Under Git Bash
the alternate reading only adds denials for strings Git Bash could not have
run anyway; under cmd/PowerShell it closes the fail-open. A guard erring
toward denial is the right direction, and this way the fix does not depend on
an answer neither the reporter nor I could establish.

Pure string work, no filesystem access, matching the rest of the guard.
"""

from __future__ import annotations

import os

#: Module constant, not an inline `os.name` test, so the platform is decided
#: in one place.
#:
#: NOT the test seam, despite being the obvious candidate: consumers bind
#: their own copy at import (`venv_install_guard._IS_WINDOWS = ...`), so
#: patching THIS name after import changes nothing and a test that flips it
#: passes while proving nothing. Flip the consuming module's constant -- that
#: is what the tests do. Caught in review, where the first version of this
#: comment claimed the opposite.
_IS_WINDOWS = os.name == "nt"


def readings(text: str, *, is_windows: bool) -> list[str]:
    """Every spelling of *text* a guard must reach a verdict on.

    Always at least the text as given, so a POSIX host is byte-for-byte
    unchanged and a Windows command with no backslash in it costs nothing.
    """
    if not is_windows or "\\" not in text:
        return [text]
    alternate = text.replace("\\", "/")
    # `C:\\` and `C:/` differ; `a\\\\b` normalises to something already seen
    # only when the text was all-backslash to begin with.
    return [text, alternate] if alternate != text else [text]
