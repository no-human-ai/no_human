"""Does the sandbox actually test ITSELF?

A bench sandbox is a copy of a repo. The agent edits the copy, runs the copy's
tests, and believes what they say. That belief is only sound if the copy's own
code is what gets imported — and for a `src/` layout it silently is not: nothing
puts `src/` on `sys.path`, so `import <pkg>` resolves through whatever editable
install exists in the active environment, which points at the ORIGINAL working
tree. The agent then edits the sandbox, runs the tests, and is shown results
from a tree it did not touch.

That is the worst feedback loop there is — a correct change appears to do
nothing — and it is invisible: every command succeeds, the tests are green or
red for real reasons, and nothing anywhere says which tree produced them. It
cost a whole large-repo bench tier before anyone noticed (2026-08-08), and the
holdout was only immune because it purges `sys.modules` and asserts its own
import, which the AGENT's test runs never do.

So the runner asks the question directly, once per spec, before the agent
starts: for each top-level package in the sandbox, import it the way the agent's
own test run would and report any that resolve OUTSIDE the sandbox.

Advisory, not fatal, and deliberately so — a repo may legitimately import an
installed sibling, and a pre-flight must never be the thing that breaks the run
it protects. It is recorded as an event and rides into the score so the question
"was this spec's feedback loop even pointed at the right tree?" has an answer in
the record instead of needing an archaeologist.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

#: Directory names that are never the subject's own package.
_NOT_PACKAGES = frozenset({
    "tests", "test", "docs", "doc", "examples", "scripts", "build", "dist",
    "node_modules", "venv", ".venv", "site-packages", "web", "desktop",
})

#: Run UNDER PYTEST, not as a bare `python -c` import, and that distinction is
#: the whole point. The agent's feedback comes from its test command, and pytest
#: applies configuration a bare import never sees — `pythonpath` in
#: `[tool.pytest.ini_options]` is exactly the setting that decides this question.
#: A bare-import version of this probe reported a repo as broken when its pytest
#: runs were in fact correct, which is a detector that would get switched off.
_PROBE = '''
import pathlib


def test_probe(capsys):
    import {name} as _m
    with capsys.disabled():
        print("NH_SELFTEST=" + str(pathlib.Path(_m.__file__).resolve()))
'''


def top_level_packages(repo: Path) -> list[str]:
    """Importable top-level package names shipped BY *repo*.

    Looks in the repo root and in ``src/`` — the two layouts that cover
    essentially everything, and the ``src/`` one is the whole reason this
    module exists.
    """
    found: list[str] = []
    for parent in (repo, repo / "src"):
        if not parent.is_dir():
            continue
        for child in sorted(parent.iterdir()):
            if (child.is_dir() and not child.name.startswith((".", "_"))
                    and child.name not in _NOT_PACKAGES
                    and (child / "__init__.py").is_file()):
                found.append(child.name)
    return sorted(set(found))


def wrong_tree_imports(work: Path, *, python: str | None = None,
                       env: dict[str, str] | None = None) -> list[str]:
    """Packages that resolve OUTSIDE *work*. Empty list = the sandbox tests itself.

    Runs each import in its own subprocess with ``cwd=work``, which is how the
    agent's own test command runs, so the answer is about the environment the
    agent actually gets rather than about this process.
    """
    problems: list[str] = []
    work = Path(work).resolve()
    for name in top_level_packages(work):
        # The probe file lives INSIDE the sandbox so pytest discovers the repo's
        # own rootdir and ini settings, and is removed immediately — the agent
        # starts afterwards and never sees it.
        probe = work / f"test_nh_sandbox_selftest_{name}.py"
        try:
            probe.write_text(_PROBE.format(name=name), encoding="utf-8")
            out = subprocess.run(
                [python or sys.executable, "-m", "pytest", "-q", "-s",
                 "-p", "no:cacheprovider", str(probe)],
                cwd=work, capture_output=True, text=True, timeout=120,
                env=env).stdout
        except (OSError, subprocess.SubprocessError):
            # A pre-flight must never be what breaks the run it protects.
            continue
        finally:
            probe.unlink(missing_ok=True)
        marker = "NH_SELFTEST="
        if marker not in out:
            continue                    # unimportable here is not this check's question
        resolved = Path(out.split(marker, 1)[1].splitlines()[0].strip())
        if not str(resolved).startswith(str(work)):
            problems.append(
                f"{name} imports from {resolved} — OUTSIDE the sandbox, so the "
                f"agent's own tests would validate that tree instead of its work")
    return problems
