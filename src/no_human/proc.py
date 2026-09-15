"""Cross-cutting facts about nh's own subprocess spawns: HOW to spawn a child
without a stray console, and WHICH interpreter to spawn as a Python.

Windows console suppression. `nh` and the CLIs it launches (`git`, `codex`)
are console-subsystem binaries. When the desktop app starts `nh` without a
console of its own (see `desktop/server.mjs`), every such child is otherwise
allocated a fresh VISIBLE console — the "multiple empty terminals" real users
reported on Windows. `CREATE_NO_WINDOW` suppresses that console;
`CREATE_NEW_PROCESS_GROUP` (only where a group is wanted) detaches the child
from our console so a Ctrl-C to nh does not also hit it. Both flags are
Windows-only; POSIX uses `start_new_session` for the group and needs nothing
to hide a console.

Interpreter resolution (`real_python`). In the PyInstaller desktop build
`sys.executable` is the frozen `nh` binary rather than a Python, so
`[sys.executable, ...]` re-enters the click CLI instead of running anything.
That mistake has now been made and fixed independently four times in this
codebase; this module is where it stops being re-written inline. It has no
no_human imports of its own, so every layer can reach it without a cycle.
"""

import shutil
import sys
from pathlib import Path

# subprocess creationflags (Windows). Ints so this module imports on any OS.
CREATE_NO_WINDOW = 0x08000000
CREATE_NEW_PROCESS_GROUP = 0x00000200

# Where a venv keeps its interpreter, POSIX and Windows. Probed in this order
# so a venv built on either shape resolves from either host.
_VENV_INTERPRETERS = (("Scripts", "python.exe"), ("bin", "python"))


def hidden_console_kwargs(
    *, new_group: bool = False, platform: str | None = None
) -> dict[str, object]:
    """Popen/subprocess.run kwargs to suppress a Windows console for a child.

    Windows: ``creationflags`` with ``CREATE_NO_WINDOW`` (plus
    ``CREATE_NEW_PROCESS_GROUP`` when ``new_group``). POSIX:
    ``{"start_new_session": True}`` when ``new_group``, else ``{}`` — no console
    to hide there. ``platform`` defaults to :data:`sys.platform`.
    """
    plat = sys.platform if platform is None else platform
    if plat == "win32":
        flags = CREATE_NO_WINDOW
        if new_group:
            flags |= CREATE_NEW_PROCESS_GROUP
        return {"creationflags": flags}
    if new_group:
        return {"start_new_session": True}
    return {}


def real_python(*venvs: Path | str | None) -> str | None:
    """A Python interpreter that can actually be shelled out to, or ``None``.

    Normally :data:`sys.executable`. But in a PyInstaller-frozen build — the
    shipped desktop app — ``sys.executable`` IS the frozen ``nh`` binary, not
    a Python, so ``[sys.executable, ...]`` re-enters the click CLI and dies in
    its own argument parser without running the thing it was handed (measured
    2026-09-14 against the shipped bundle: ``nh -m pytest -q`` ->
    ``Error: No such option '-m'``; ``nh scripts/check_release_manifest.py``
    -> ``Error: No such command``). The failure is silent in the worst way —
    a non-zero exit that reads as "your tests failed" rather than "nothing
    ran".

    Fourth site of one scar, which is why it lives here once instead of a
    fourth time inline: ``testing/repro_gate.py::_pytest_python`` carries it
    for the repro gate (where it produced a confident but FALSE verdict),
    ``core/worktree.py::_builder_python`` for venv creation, and
    ``vcs/approve_merge.py::_real_python`` for the merge-time gate. All three
    now delegate here.

    *venvs* are virtualenv ROOT directories to prefer, in order, before PATH —
    a target repo's own venv has that repo's dependencies, which a bare
    ``python3`` does not. ``None`` entries are skipped, as is any candidate
    that is not a file on disk. Then ``python3``/``python`` on PATH.

    Returns ``None`` only in a frozen build with no interpreter anywhere. Every
    caller must fail closed on it and say so, because the one thing worse than
    "no interpreter" is quietly handing the argv to the CLI instead.
    """
    if not getattr(sys, "frozen", False):
        return sys.executable
    for venv in venvs:
        if venv is None:
            continue
        for sub, name in _VENV_INTERPRETERS:
            candidate = Path(venv) / sub / name
            if candidate.is_file():
                return str(candidate)
    for name in ("python3", "python"):
        found = shutil.which(name)
        if found:
            return found
    return None
